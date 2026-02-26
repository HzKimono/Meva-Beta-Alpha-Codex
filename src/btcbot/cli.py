from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from btcbot.accounting.accounting_service import AccountingService
from btcbot.adapters.btcturk_http import (
    BtcturkHttpClient,
    ConfigurationError,
)
from btcbot.config import Settings
from btcbot.domain.anomalies import AnomalyCode, AnomalyEvent
from btcbot.domain.models import PairInfo, normalize_symbol
from btcbot.logging_context import with_logging_context
from btcbot.logging_utils import setup_logging
from btcbot.obs.logging import set_base_context
from btcbot.obs.process_role import ProcessRole, coerce_process_role
from btcbot.observability import (
    configure_instrumentation,
    flush_instrumentation,
    get_instrumentation,
)
from btcbot.observability_decisions import emit_decision
from btcbot.persistence.sqlite.sqlite_connection import sqlite_connection_context
from btcbot.replay import ReplayCaptureConfig, capture_replay_dataset, init_replay_dataset
from btcbot.replay.validate import validate_replay_dataset
from btcbot.risk.exchange_rules import MarketDataExchangeRulesProvider
from btcbot.risk.policy import RiskPolicy
from btcbot.runtime.guards import (
    enforce_role_db_convention,
    normalize_db_path,
    require_no_dotenv,
)
from btcbot.security.redaction import redact_data
from btcbot.security.secrets import (
    build_default_provider,
    enforce_secret_rotation_hygiene,
    inject_runtime_secrets,
    log_secret_validation,
    validate_secret_controls,
)
from btcbot.services.cycle_account_snapshot import build_cycle_account_snapshot
from btcbot.services.doctor import (
    DoctorReport,
    doctor_status,
    evaluate_slo_status_for_rows,
    normalize_drawdown_ratio,
    run_health_checks,
)
from btcbot.services.effective_universe import resolve_effective_universe
from btcbot.services.exchange_factory import build_exchange_stage3
from btcbot.services.execution_service import ExecutionService
from btcbot.services.market_data_replay import MarketDataReplay
from btcbot.services.market_data_service import MarketDataService
from btcbot.services.parity import (
    compare_fingerprints,
    compute_run_fingerprint,
    find_missing_stage7_parity_tables,
)
from btcbot.services.portfolio_service import PortfolioService
from btcbot.services.preflight import run_preflight_checks
from btcbot.services.process_lock import single_instance_lock
from btcbot.services.risk_service import RiskService
from btcbot.services.stage4_cycle_runner import (
    Stage4ConfigurationError,
    Stage4CycleRunner,
    Stage4ExchangeError,
    Stage4InvariantError,
)
from btcbot.services.stage7_backtest_runner import Stage7BacktestRunner
from btcbot.services.stage7_cycle_runner import Stage7CycleRunner
from btcbot.services.stage7_reporting import (
    build_cycle_rows,
    validate_cycle_rows,
)
from btcbot.services.stage7_reporting import (
    render_csv as render_stage7_csv,
)
from btcbot.services.stage7_reporting import (
    render_json as render_stage7_json,
)
from btcbot.services.stage7_reporting import (
    rollup as build_stage7_rollup,
)
from btcbot.services.startup_recovery import StartupRecoveryService
from btcbot.services.state_store import PENDING_GRACE_SECONDS, StateStore
from btcbot.services.strategy_service import StrategyService
from btcbot.services.sweep_service import SweepService
from btcbot.services.trading_policy import validate_live_side_effects_policy
from btcbot.strategies.profit_v1 import ProfitAwareStrategyV1

logger = logging.getLogger(__name__)

LIVE_TRADING_NOT_ARMED_MESSAGE = (
    "Live trading is not armed; set LIVE_TRADING=true and LIVE_TRADING_ACK=I_UNDERSTAND"
)


def _canonical_process_role(settings: Settings) -> str:
    return coerce_process_role(getattr(settings, "process_role", None)).value


def _should_stop_loop_if_killed(*, state_store: StateStore, settings: Settings) -> bool:
    process_role = _canonical_process_role(settings)
    if process_role != ProcessRole.LIVE.value:
        return False
    enabled, _reason, _until = state_store.get_kill_switch(process_role)
    return bool(enabled)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="btcbot",
        epilog=(
            "Env overrides: UNIVERSE_SYMBOLS (or legacy SYMBOLS), TRY_CASH_TARGET, "
            "UNIVERSE_AUTO_CORRECT. "
            "PowerShell quickstart: stage7-backtest --dataset ./data/replay "
            "--out ./backtest.db ... | "
            "stage7-parity --out-a ./a.db --out-b ./b.db ... | "
            "stage7-backtest-report --db ./backtest.db --out out.jsonl | "
            "stage7-db-count --db ./backtest.db"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "Dotenv bootstrap is forbidden in production runs. "
            "Do not pass --env-file; use environment variables instead."
        ),
    )

    run_parser = subparsers.add_parser("run", help="Run one decision cycle")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not place orders")
    run_parser.add_argument("--loop", action="store_true", help="Run continuously")
    run_parser.add_argument("--once", action="store_true", help="Alias for single cycle")
    run_parser.add_argument(
        "--cycle-seconds",
        "--sleep-seconds",
        dest="cycle_seconds",
        type=int,
        default=10,
        help="Sleep seconds between cycles (alias: --sleep-seconds)",
    )
    run_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Maximum cycles (default: infinite in --loop mode; use -1 for infinite)",
    )
    run_parser.add_argument(
        "--jitter-seconds", type=int, default=0, help="Optional random jitter added to cycle sleep"
    )

    canary_parser = subparsers.add_parser("canary", help="Guarded live canary workflows")
    canary_subparsers = canary_parser.add_subparsers(dest="canary_command", required=True)

    canary_once_parser = canary_subparsers.add_parser(
        "once", help="Single-cycle live canary (Stage 2 equivalent)"
    )
    canary_loop_parser = canary_subparsers.add_parser(
        "loop", help="Bounded live canary loop (Stage 3 equivalent)"
    )

    for canary_mode_parser in (canary_once_parser, canary_loop_parser):
        canary_mode_parser.add_argument(
            "--symbol",
            default=None,
            help="Single canary symbol (defaults to configured symbol only when exactly one exists)",
        )
        canary_mode_parser.add_argument(
            "--notional-try",
            type=Decimal,
            default=Decimal("150"),
            help="Canary notional cap TRY per cycle and per order",
        )
        canary_mode_parser.add_argument(
            "--cycle-seconds",
            type=int,
            default=10,
            help="Sleep seconds between canary cycles",
        )
        canary_mode_parser.add_argument(
            "--ttl-seconds",
            type=int,
            default=30,
            help="Forced order TTL in canary mode",
        )
        canary_mode_parser.add_argument(
            "--db-path",
            default=None,
            help="State sqlite DB path (defaults to env STATE_DB_PATH)",
        )
        canary_mode_parser.add_argument(
            "--market-data-mode",
            choices=["rest", "ws"],
            default=None,
            help="Optional market data mode override for canary only",
        )
        canary_mode_parser.add_argument(
            "--allow-warn",
            action="store_true",
            help="Allow doctor WARN status to proceed",
        )
        canary_mode_parser.add_argument(
            "--export-out",
            default=None,
            help="Optional JSONL export path for the last canary Stage 7 rows",
        )
        canary_mode_parser.add_argument(
            "--json",
            action="store_true",
            help="Print canary summary as machine-readable JSON",
        )

    canary_loop_parser.add_argument(
        "--max-cycles",
        type=int,
        default=60,
        help="Maximum canary loop cycles",
    )

    stage4_run_parser = subparsers.add_parser("stage4-run", help="Run one Stage 4 cycle")
    stage4_run_parser.add_argument("--dry-run", action="store_true", help="Do not place orders")
    stage4_run_parser.add_argument("--loop", action="store_true", help="Run continuously")
    stage4_run_parser.add_argument("--once", action="store_true", help="Alias for single cycle")
    stage4_run_parser.add_argument(
        "--cycle-seconds",
        "--sleep-seconds",
        dest="cycle_seconds",
        type=int,
        default=10,
        help="Sleep seconds between cycles (alias: --sleep-seconds)",
    )
    stage4_run_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Maximum cycles (default: infinite in --loop mode; use -1 for infinite)",
    )
    stage4_run_parser.add_argument(
        "--jitter-seconds", type=int, default=0, help="Optional random jitter added to cycle sleep"
    )
    stage4_run_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )

    stage4_freeze_status_parser = subparsers.add_parser(
        "stage4-freeze-status",
        help="Show Stage 4 unknown-order freeze state",
    )
    stage4_freeze_status_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )

    stage4_freeze_clear_parser = subparsers.add_parser(
        "stage4-freeze-clear",
        help="Clear Stage 4 unknown-order freeze state",
    )
    stage4_freeze_clear_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    stage4_freeze_clear_parser.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation flag to clear freeze state",
    )

    degrade_parser = subparsers.add_parser("degrade", help="Manage degraded mode state")
    degrade_subparsers = degrade_parser.add_subparsers(dest="degrade_command", required=True)

    degrade_status_parser = degrade_subparsers.add_parser("status", help="Show degraded mode status")
    degrade_status_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )

    degrade_clear_parser = degrade_subparsers.add_parser("clear", help="Reset degraded mode state")
    degrade_clear_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )

    degrade_override_parser = degrade_subparsers.add_parser(
        "override", help="Manually set degraded mode override"
    )
    degrade_override_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    degrade_override_parser.add_argument(
        "--mode",
        required=True,
        choices=["OBSERVE_ONLY", "REDUCE_RISK_ONLY", "NORMAL"],
        help="Manual override mode",
    )
    degrade_override_parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=900,
        help="Cooldown duration in seconds",
    )
    degrade_override_parser.add_argument(
        "--reason",
        required=True,
        help="Operator reason for override",
    )


    db_locks_parser = subparsers.add_parser("state-db-locks", help="List state DB process locks")
    db_locks_parser.add_argument(
        "list_command",
        nargs="?",
        default="list",
        choices=["list"],
        help="List process lock rows",
    )
    db_locks_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )

    db_unlock_parser = subparsers.add_parser("state-db-unlock", help="Safely release stale state DB process lock")
    db_unlock_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    db_unlock_parser.add_argument("--instance-id", required=True, help="Target process instance id")
    db_unlock_parser.add_argument("--force", action="store_true", help="Force unlock even if heartbeat is fresh")
    db_unlock_parser.add_argument(
        "--force-ack",
        default=None,
        help="Required with --force: set to I_UNDERSTAND_STATE_DB_UNLOCK",
    )

    stage7_run_parser = subparsers.add_parser("stage7-run", help="Run one Stage 7 dry-run cycle")
    stage7_run_parser.add_argument("--dry-run", action="store_true", help="Required for stage7")
    stage7_run_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    stage7_run_parser.add_argument(
        "--include-adaptation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable adaptation evaluation and parameter persistence during this cycle",
    )

    subparsers.add_parser("health", help="Check exchange connectivity")

    report_parser = subparsers.add_parser(
        "stage7-report",
        help="Print recent Stage 7 metrics with canonical cycles/rollups/validations schema",
    )
    report_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    report_parser.add_argument("--last", type=int, default=10)
    report_parser.add_argument(
        "--json",
        action="store_true",
        help="Deprecated alias for --json-only (print JSON report payload only)",
    )
    report_parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only canonical JSON report payload (cycles, rollups, validations)",
    )

    export_parser = subparsers.add_parser(
        "stage7-export",
        help="Export Stage 7 canonical report schema as CSV/JSON/both (stdout if --out omitted)",
    )
    export_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    export_parser.add_argument("--last", type=int, default=50)
    export_parser.add_argument("--format", choices=["csv", "json", "both", "jsonl"], default="csv")
    export_parser.add_argument("--out", required=False, default=None)

    alerts_parser = subparsers.add_parser("stage7-alerts", help="Print recent Stage 7 alert cycles")
    alerts_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    alerts_parser.add_argument("--last", type=int, default=50)

    backtest_parser = subparsers.add_parser("stage7-backtest", help="Run Stage 7 replay backtest")
    backtest_parser.add_argument(
        "--data",
        "--dataset",
        dest="data",
        required=False,
        default=None,
        help=(
            "Path to replay dataset folder (alias: --dataset). "
            "Defaults to env BTCTBOT_REPLAY_DATASET or ./data/replay if it exists"
        ),
    )
    backtest_parser.add_argument(
        "--out-db",
        "--out",
        dest="out_db",
        required=True,
        help="Output sqlite DB path (alias: --out)",
    )
    backtest_parser.add_argument("--start", required=True)
    backtest_parser.add_argument("--end", required=True)
    backtest_parser.add_argument("--step-seconds", type=int, default=60)
    backtest_parser.add_argument("--seed", type=int, default=123)
    backtest_parser.add_argument("--cycles", type=int, default=None)
    backtest_parser.add_argument(
        "--pair-info-json",
        default=None,
        help="Optional JSON file with exchange pair metadata for replay parity",
    )
    backtest_parser.add_argument(
        "--include-adaptation",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable adaptation evaluation/persistence during backtest cycles. "
            "By default backtests freeze params and disable adaptation."
        ),
    )

    parity_parser = subparsers.add_parser("stage7-parity", help="Compare two Stage 7 run DBs")
    parity_parser.add_argument("--db-a", "--out-a", dest="db_a", required=True)
    parity_parser.add_argument("--db-b", "--out-b", dest="db_b", required=True)
    parity_parser.add_argument("--data", "--dataset", dest="dataset")
    parity_parser.add_argument("--start", required=True)
    parity_parser.add_argument("--end", required=True)
    parity_parser.add_argument(
        "--quantize-try",
        default=None,
        help="Optional TRY quantization step for fingerprint metrics (e.g. 0.01)",
    )
    parity_parser.add_argument(
        "--include-adaptation",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Include adaptation metadata tables/columns in parity fingerprint only "
            "(does not run adaptation)."
        ),
    )

    preflight_parser = subparsers.add_parser("preflight", help="Run fail-fast live/canary readiness checks")
    preflight_parser.add_argument(
        "--db",
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    preflight_parser.add_argument(
        "--profile",
        choices=["live", "dry-run"],
        default="live",
        help="Preflight profile: strict live checks or dry-run readiness",
    )

    doctor_parser = subparsers.add_parser("doctor", help="Validate local config, env, and DB")
    doctor_parser.add_argument(
        "--db", default=None, help="Sqlite DB path (defaults to env STATE_DB_PATH)"
    )
    doctor_parser.add_argument(
        "--dataset",
        default=None,
        help="Optional replay dataset folder path to validate for backtests",
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON report"
    )
    doctor_parser.add_argument(
        "--allow-mismatched-db",
        action="store_true",
        help="Allow doctor to run even when role/db naming convention mismatches",
    )

    replay_init_parser = subparsers.add_parser(
        "replay-init", help="Initialize replay dataset structure"
    )
    replay_init_parser.add_argument("--dataset", required=True, help="Replay dataset folder path")
    replay_init_parser.add_argument(
        "--seed", type=int, default=123, help="Deterministic seed for synthetic sample"
    )
    replay_init_parser.add_argument(
        "--no-synthetic",
        action="store_true",
        help="Only create folder/schema docs; do not write synthetic sample files",
    )

    replay_capture_parser = subparsers.add_parser(
        "replay-capture",
        help="Capture replay dataset from BTCTurk public endpoints",
    )
    replay_capture_parser.add_argument(
        "--dataset", required=True, help="Replay dataset folder path"
    )
    replay_capture_parser.add_argument(
        "--symbols", required=True, help="Comma-separated symbols e.g. BTCTRY,ETHTRY"
    )
    replay_capture_parser.add_argument(
        "--seconds", type=int, default=300, help="Capture duration in seconds"
    )
    replay_capture_parser.add_argument(
        "--interval-seconds",
        type=int,
        default=1,
        help="Delay between capture polls in seconds",
    )

    backtest_export = subparsers.add_parser(
        "stage7-backtest-export",
        aliases=["stage7-backtest-report"],
        help="Export backtest rows from a Stage 7 DB",
    )
    backtest_export.add_argument(
        "--db",
        required=False,
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )
    backtest_export.add_argument("--out", required=False, default=None)
    backtest_export.add_argument("--last", type=int, default=50)
    backtest_export.add_argument("--format", choices=["csv", "json", "both", "jsonl"], default="csv")

    backtest_count = subparsers.add_parser(
        "stage7-db-count",
        help="Print row counts for Stage 7 tables in a sqlite DB",
    )
    backtest_count.add_argument(
        "--db",
        required=False,
        default=None,
        help="State sqlite DB path (defaults to env STATE_DB_PATH)",
    )

    args = parser.parse_args()
    require_no_dotenv(args.env_file)
    settings = _load_settings(args.env_file)
    settings = _prepare_runtime(
        settings,
        command_name=args.command,
        env_file_arg=args.env_file,
        db_override=getattr(args, "db", None),
        prefer_env_db=args.command == "stage7-db-count" and getattr(args, "db", None) is None,
    )
    setup_logging(settings.log_level)
    if args.command != "run":
        configure_instrumentation(
            enabled=bool(getattr(settings, "observability_enabled", False)),
            metrics_exporter=str(getattr(settings, "observability_metrics_exporter", "none")),
            otlp_endpoint=getattr(settings, "observability_otlp_endpoint", None),
            prometheus_port=int(getattr(settings, "observability_prometheus_port", 9464)),
        )
    settings = _apply_effective_universe(settings)
    set_base_context(process_role=settings.process_role, state_db_path=settings.state_db_path)
    logger.info(
        "runtime_prepared",
        extra={
            "extra": {
                "role": settings.process_role,
                "db_path": settings.state_db_path,
                "live_trading": bool(getattr(settings, "live_trading", False)),
                "safe_mode": bool(getattr(settings, "safe_mode", False)),
                "kill_switch": bool(getattr(settings, "kill_switch", False)),
                "pid": os.getpid(),
                "command": args.command,
            }
        },
    )

    if args.command in {"run", "stage4-run"}:
        _print_effective_side_effects_state(
            settings,
            force_dry_run=bool(getattr(args, "dry_run", False)),
            include_safe_mode=True,
        )

    if args.command == "run":
        return run_stage3_runtime(
            settings,
            force_dry_run=args.dry_run,
            loop_enabled=args.loop and not args.once,
            cycle_seconds=args.cycle_seconds,
            max_cycles=args.max_cycles,
            jitter_seconds=args.jitter_seconds,
        )

    if args.command == "canary":
        return run_canary(
            settings,
            mode=args.canary_command,
            symbol=args.symbol,
            notional_try=args.notional_try,
            cycle_seconds=args.cycle_seconds,
            max_cycles=getattr(args, "max_cycles", None),
            ttl_seconds=args.ttl_seconds,
            db_path=args.db_path,
            market_data_mode=args.market_data_mode,
            allow_warn=args.allow_warn,
            export_out=args.export_out,
            json_output=args.json,
        )

    if args.command == "stage4-run":
        return run_with_optional_loop(
            command="stage4-run",
            cycle_fn=lambda: run_cycle_stage4(
                settings, force_dry_run=args.dry_run, db_path=args.db
            ),
            loop_enabled=args.loop and not args.once,
            cycle_seconds=args.cycle_seconds,
            max_cycles=args.max_cycles,
            jitter_seconds=args.jitter_seconds,
        )

    if args.command == "stage4-freeze-status":
        return run_stage4_freeze_status(settings=settings, db_path=args.db)

    if args.command == "stage4-freeze-clear":
        return run_stage4_freeze_clear(settings=settings, db_path=args.db, confirmed=args.yes)

    if args.command == "degrade":
        if args.degrade_command == "status":
            return run_degrade_status(settings=settings, db_path=args.db)
        if args.degrade_command == "clear":
            return run_degrade_clear(settings=settings, db_path=args.db)
        if args.degrade_command == "override":
            return run_degrade_override(
                settings=settings,
                db_path=args.db,
                mode=args.mode,
                cooldown_seconds=args.cooldown_seconds,
                reason=args.reason,
            )

    if args.command == "state-db-locks":
        return run_state_db_locks_list(settings=settings, db_path=args.db)

    if args.command == "state-db-unlock":
        return run_state_db_unlock(
            settings=settings,
            db_path=args.db,
            instance_id=args.instance_id,
            force=bool(args.force),
            force_ack=args.force_ack,
        )

    if args.command == "stage7-run":
        return run_cycle_stage7(
            settings,
            force_dry_run=args.dry_run,
            include_adaptation=args.include_adaptation,
            db_path=args.db,
        )

    if args.command == "health":
        return run_health(settings)

    if args.command == "stage7-report":
        return run_stage7_report(
            settings,
            db_path=args.db,
            last=args.last,
            json_output=args.json,
            json_only=getattr(args, "json_only", False),
        )

    if args.command == "stage7-export":
        return run_stage7_export(
            settings,
            db_path=args.db,
            last=args.last,
            export_format=args.format,
            out_path=args.out,
        )

    if args.command == "stage7-alerts":
        return run_stage7_alerts(settings, db_path=args.db, last=args.last)

    if args.command == "stage7-backtest":
        return run_stage7_backtest(
            settings,
            data_path=args.data,
            out_db=args.out_db,
            start=args.start,
            end=args.end,
            step_seconds=args.step_seconds,
            seed=args.seed,
            cycles=args.cycles,
            pair_info_json=args.pair_info_json,
            include_adaptation=args.include_adaptation,
        )

    if args.command == "stage7-parity":
        return run_stage7_parity(
            db_a=args.db_a,
            db_b=args.db_b,
            start=args.start,
            end=args.end,
            dataset=args.dataset,
            quantize_try=args.quantize_try,
            include_adaptation=args.include_adaptation,
        )

    if args.command in {"stage7-backtest-export", "stage7-backtest-report"}:
        return run_stage7_backtest_export(
            settings=settings,
            db_path=args.db,
            last=args.last,
            export_format=args.format,
            out_path=args.out,
            explicit_last=_argument_was_provided("--last"),
        )

    if args.command == "stage7-db-count":
        return run_stage7_db_count(settings=settings, db_path=args.db)

    if args.command == "preflight":
        return run_preflight(settings=settings, db_path=args.db, profile=args.profile)

    if args.command == "doctor":
        # Doctor remains runnable without DB on first-time setup; SLO coverage then warns/skips.
        resolved_db_path = _resolve_stage7_db_path(
            "doctor",
            db_path=args.db,
            settings_db_path=None,
            silent=True,
        )
        if resolved_db_path:
            try:
                enforce_role_db_convention(
                    getattr(settings, "process_role", ""),
                    bool(getattr(settings, "live_trading", False)),
                    normalize_db_path(resolved_db_path),
                )
            except ValueError as exc:
                if not bool(args.allow_mismatched_db):
                    print(str(exc))
                    return 2
                logger.warning(
                    "doctor_role_db_convention_bypassed",
                    extra={
                        "extra": {
                            "db_path": resolved_db_path,
                            "role": getattr(settings, "process_role", ""),
                        }
                    },
                )
        return run_doctor(
            settings=settings,
            db_path=resolved_db_path,
            dataset_path=args.dataset,
            json_output=args.json,
        )

    if args.command == "replay-init":
        return run_replay_init(
            dataset_path=args.dataset,
            seed=args.seed,
            write_synthetic=not args.no_synthetic,
        )

    if args.command == "replay-capture":
        return run_replay_capture(
            dataset_path=args.dataset,
            symbols_csv=args.symbols,
            seconds=args.seconds,
            interval_seconds=args.interval_seconds,
        )

    return 1


def _command_touches_state_db(command_name: str) -> bool:
    return command_name in {
        "run",
        "canary",
        "stage4-run",
        "stage4-freeze-status",
        "stage4-freeze-clear",
        "degrade",
        "state-db-locks",
        "state-db-unlock",
        "stage7-run",
        "health",
        "stage7-report",
        "stage7-export",
        "stage7-alerts",
        "stage7-backtest-export",
        "stage7-backtest-report",
        "stage7-db-count",
        "preflight",
    }


def _enforce_role_db_convention_for_command(command_name: str) -> bool:
    return command_name in {
        "run",
        "canary",
        "stage4-run",
        "stage4-freeze-status",
        "stage4-freeze-clear",
        "degrade",
        "state-db-locks",
        "state-db-unlock",
        "health",
        "stage7-report",
        "stage7-export",
        "stage7-alerts",
        "stage7-db-count",
        "stage7-backtest-export",
        "stage7-backtest-report",
        "doctor",
        "preflight",
    }


def _strict_role_db_convention_for_command(command_name: str) -> bool:
    return command_name in {"run", "canary", "stage4-run", "health"}


def _prepare_runtime(
    settings: Settings,
    *,
    command_name: str,
    env_file_arg: str | None,
    db_override: str | None = None,
    prefer_env_db: bool = False,
) -> Settings:
    require_no_dotenv(env_file_arg)
    if _command_touches_state_db(command_name):
        effective_db = db_override.strip() if db_override and db_override.strip() else None
        if effective_db is None and prefer_env_db:
            env_db = os.getenv("STATE_DB_PATH")
            effective_db = env_db.strip() if env_db and env_db.strip() else None
        if effective_db is not None:
            if hasattr(settings, "model_copy"):
                settings = settings.model_copy(update={"state_db_path": effective_db})
            else:
                settings.state_db_path = effective_db

        db_path = normalize_db_path(settings.state_db_path)
        if hasattr(settings, "model_copy"):
            settings = settings.model_copy(update={"state_db_path": str(db_path)})
        else:
            settings.state_db_path = str(db_path)
        if _enforce_role_db_convention_for_command(command_name):
            try:
                enforce_role_db_convention(
                    getattr(settings, "process_role", ""),
                    bool(getattr(settings, "live_trading", False)),
                    db_path,
                )
            except ValueError:
                if _strict_role_db_convention_for_command(command_name):
                    raise
                logger.warning(
                    "role_db_convention_mismatch",
                    extra={
                        "extra": {
                            "command": command_name,
                            "role": getattr(settings, "process_role", ""),
                            "db_path": str(db_path),
                        }
                    },
                )

    logger.info(
        "startup",
        extra={
            "extra": {
                "role": getattr(settings, "process_role", ""),
                "db_path": getattr(settings, "state_db_path", ""),
                "live_trading": bool(getattr(settings, "live_trading", False)),
                "safe_mode": bool(getattr(settings, "safe_mode", False)),
                "kill_switch": bool(getattr(settings, "kill_switch", False)),
                "pid": os.getpid(),
                "command": command_name,
            }
        },
    )
    return settings


def _load_settings(env_file: str | None) -> Settings:
    resolved_env_file = None if env_file in (None, "") else env_file
    provider = build_default_provider(env_file=resolved_env_file)
    inject_runtime_secrets(
        provider,
        keys=("BTCTURK_API_KEY", "BTCTURK_API_SECRET", "LIVE_TRADING_ACK"),
    )

    if resolved_env_file is None:
        settings = Settings()
    else:
        try:
            settings = Settings(_env_file=resolved_env_file)
        except TypeError:
            settings = Settings()
    validation = validate_secret_controls(
        scopes=list(getattr(settings, "btcturk_api_scopes", ["read", "trade"])),
        rotated_at=getattr(settings, "btcturk_secret_rotated_at", None),
        max_age_days=int(getattr(settings, "btcturk_secret_max_age_days", 90)),
        live_trading=bool(getattr(settings, "live_trading", False)),
    )
    log_secret_validation(validation)
    if not validation.ok:
        raise ValueError("Secret controls validation failed")
    rotated_at_raw = getattr(settings, "api_key_rotated_at", None)
    policy_blocked = enforce_secret_rotation_hygiene(
        api_key_rotated_at=rotated_at_raw,
        warn_days=int(getattr(settings, "secret_rotation_warn_days", 30)),
        max_age_days=int(getattr(settings, "secret_max_age_days", 90)),
    )
    rotation_age_days: int | None = None
    if rotated_at_raw:
        try:
            rotated_date = datetime.strptime(str(rotated_at_raw), "%Y-%m-%d").date()
            rotation_age_days = (datetime.now(UTC).date() - rotated_date).days
        except ValueError:
            rotation_age_days = None
    logger.info(
        "secret_policy_evaluation_summary",
        extra={
            "extra": {
                "rotation_age_days": rotation_age_days,
                "rotation_blocked": bool(policy_blocked),
                "btcturk_controls_ok": bool(validation.ok),
                "btcturk_errors_count": len(validation.errors),
                "btcturk_warnings_count": len(validation.warnings),
                "live_trading": bool(getattr(settings, "live_trading", False)),
            }
        },
    )
    return settings


def run_with_optional_loop(
    *,
    command: str,
    cycle_fn: Callable[[], int],
    loop_enabled: bool,
    cycle_seconds: int,
    max_cycles: int | None,
    jitter_seconds: int,
    stop_loop_fn: Callable[[], bool] | None = None,
    idle_hook_fn: Callable[[], None] | None = None,
) -> int:
    if cycle_seconds < 0 or jitter_seconds < 0:
        print("cycle-seconds and jitter-seconds must be >= 0")
        return 2
    if max_cycles is not None and (max_cycles == 0 or max_cycles < -1):
        print("max-cycles must be >= 1, or -1 for infinite")
        return 2
    if max_cycles == -1:
        max_cycles = None

    if not loop_enabled:
        return cycle_fn()

    cycle = 0
    last_rc = 0
    logger.info(
        "loop_runner_started",
        extra={
            "extra": {
                "command": command,
                "cycle_seconds": cycle_seconds,
                "max_cycles": max_cycles,
                "jitter_seconds": jitter_seconds,
            }
        },
    )
    try:
        while True:
            cycle += 1
            attempt = 0
            while True:
                attempt += 1
                try:
                    last_rc = cycle_fn()
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if attempt >= 3:
                        logger.exception(
                            "loop_cycle_failed",
                            extra={
                                "extra": {
                                    "command": command,
                                    "cycle": cycle,
                                    "attempt": attempt,
                                    "error_type": type(exc).__name__,
                                }
                            },
                        )
                        last_rc = 1
                        break
                    backoff = min(8, 2 ** (attempt - 1))
                    logger.warning(
                        "loop_cycle_retrying",
                        extra={
                            "extra": {
                                "command": command,
                                "cycle": cycle,
                                "attempt": attempt,
                                "sleep_seconds": backoff,
                                "error_type": type(exc).__name__,
                            }
                        },
                    )
                    sleep_until = time.monotonic() + backoff
                    while time.monotonic() < sleep_until:
                        time.sleep(min(1.0, max(0.0, sleep_until - time.monotonic())))
                        if callable(idle_hook_fn):
                            idle_hook_fn()

            if callable(stop_loop_fn) and stop_loop_fn():
                logger.warning(
                    "loop_runner_stop_requested",
                    extra={"extra": {"command": command, "cycle": cycle}},
                )
                return last_rc

            if max_cycles is not None and cycle >= max_cycles:
                logger.info(
                    "loop_runner_completed",
                    extra={"extra": {"command": command, "cycles": cycle, "last_rc": last_rc}},
                )
                return last_rc

            sleep_for = cycle_seconds + (
                random.randint(0, jitter_seconds) if jitter_seconds > 0 else 0
            )
            if sleep_for <= 0:
                sleep_for = 1
            sleep_until = time.monotonic() + sleep_for
            while time.monotonic() < sleep_until:
                time.sleep(min(1.0, max(0.0, sleep_until - time.monotonic())))
                if callable(idle_hook_fn):
                    idle_hook_fn()
    except KeyboardInterrupt:
        logger.info(
            "loop_runner_stopped",
            extra={
                "extra": {
                    "command": command,
                    "cycles": cycle,
                    "last_rc": last_rc,
                    "reason": "keyboard_interrupt",
                }
            },
        )
        print(f"{command}: interrupted, shutting down cleanly")
        return last_rc


def _build_state_store(
    db_path: str,
    *,
    strict_instance_lock: bool,
    settings: Settings | None = None,
) -> StateStore:
    resolved_settings = settings or Settings()
    try:
        return StateStore(
            db_path=db_path,
            strict_instance_lock=strict_instance_lock,
            process_instance_ttl_seconds=int(getattr(resolved_settings, "process_instance_ttl_seconds", 180)),
        )
    except TypeError:
        return StateStore(db_path=db_path)


def run_stage3_runtime(
    settings: Settings,
    *,
    force_dry_run: bool,
    loop_enabled: bool,
    cycle_seconds: int,
    max_cycles: int | None,
    jitter_seconds: int,
) -> int:
    try:
        with single_instance_lock(db_path=settings.state_db_path, account_key="stage3"):
            configure_instrumentation(
                enabled=bool(getattr(settings, "observability_enabled", False)),
                metrics_exporter=str(getattr(settings, "observability_metrics_exporter", "none")),
                otlp_endpoint=getattr(settings, "observability_otlp_endpoint", None),
                prometheus_port=int(getattr(settings, "observability_prometheus_port", 9464)),
            )
            _, runtime_live_policy = _compute_live_policy(
                settings,
                force_dry_run=force_dry_run,
                include_safe_mode=True,
            )
            live_armed = bool(
                (force_dry_run is False)
                and bool(getattr(settings, "live_trading", False))
                and (str(getattr(settings, "live_trading_ack", "")) == "I_UNDERSTAND")
                and (not bool(getattr(settings, "kill_switch", False)))
            )
            strict_lock = True if live_armed else bool(getattr(settings, "state_db_strict_lock", True))
            try:
                runtime_state_store = _build_state_store(
                    settings.state_db_path,
                    strict_instance_lock=strict_lock,
                    settings=settings,
                )
            except TypeError:
                runtime_state_store = _build_state_store(
                    settings.state_db_path,
                    strict_instance_lock=strict_lock,
                )
            logger.info(
                "state_store_runtime_owner",
                extra={
                    "extra": {
                        "db_path": getattr(
                            runtime_state_store, "db_path_abs", settings.state_db_path
                        ),
                        "instance_id": getattr(runtime_state_store, "instance_id", ""),
                        "strict_lock": strict_lock,
                        "heartbeat_interval_seconds": int(
                            getattr(settings, "process_instance_heartbeat_interval_seconds", 30)
                        ),
                    }
                },
            )
            heartbeat_interval_seconds = max(
                1,
                int(getattr(settings, "process_instance_heartbeat_interval_seconds", 30)),
            )
            next_heartbeat_at = time.monotonic() + heartbeat_interval_seconds
            heartbeat_error_count = 0

            def _heartbeat_idle_hook() -> None:
                nonlocal next_heartbeat_at, heartbeat_error_count
                if time.monotonic() < next_heartbeat_at:
                    return
                heartbeat_instance_lock = getattr(runtime_state_store, "heartbeat_instance_lock", None)
                if not callable(heartbeat_instance_lock):
                    return
                try:
                    heartbeat_instance_lock()
                    heartbeat_error_count = 0
                except Exception as exc:  # noqa: BLE001
                    heartbeat_error_count += 1
                    get_instrumentation().counter("heartbeat_failures", 1)
                    if heartbeat_error_count <= 1 or heartbeat_error_count % 10 == 0:
                        logger.warning(
                            "instance_heartbeat_failure",
                            extra={
                                "extra": {
                                    "error": str(exc),
                                    "error_type": type(exc).__name__,
                                    "heartbeat_error_count": heartbeat_error_count,
                                }
                            },
                        )
                next_heartbeat_at = time.monotonic() + heartbeat_interval_seconds
            def _stop_loop_if_killed() -> bool:
                return _should_stop_loop_if_killed(state_store=runtime_state_store, settings=settings)

            return run_with_optional_loop(
                command="run",
                cycle_fn=lambda: run_cycle(
                    settings,
                    force_dry_run=force_dry_run,
                    state_store=runtime_state_store,
                ),
                loop_enabled=loop_enabled,
                cycle_seconds=cycle_seconds,
                max_cycles=max_cycles,
                jitter_seconds=jitter_seconds,
                stop_loop_fn=_stop_loop_if_killed,
                idle_hook_fn=_heartbeat_idle_hook,
            )
    except RuntimeError as exc:
        logger.error("stage3_runtime_lock_acquire_failed", extra={"extra": {"error": str(exc)}})
        print(str(exc))
        return 2


def _resolve_canary_symbol(settings: Settings, requested_symbol: str | None) -> str | None:
    if requested_symbol:
        return normalize_symbol(requested_symbol)
    symbols = [normalize_symbol(symbol) for symbol in getattr(settings, "symbols", []) if symbol]
    if len(symbols) == 1:
        return symbols[0]
    return None


def _build_canary_settings(
    settings: Settings,
    *,
    symbol: str,
    notional_try: Decimal,
    ttl_seconds: int,
    db_path: str,
    market_data_mode: str | None,
) -> Settings:
    overrides: dict[str, object] = {
        "symbols": [symbol],
        "max_orders_per_cycle": 1,
        "max_open_orders_per_symbol": 1,
        "notional_cap_try_per_cycle": notional_try,
        "max_notional_per_order_try": notional_try,
        "ttl_seconds": ttl_seconds,
        "state_db_path": db_path,
        "process_role": ProcessRole.LIVE.value,
    }
    if market_data_mode:
        overrides["market_data_mode"] = market_data_mode
    return settings.model_copy(update=overrides)


def _check_canary_min_notional(
    settings: Settings, symbol: str, requested_notional: Decimal
) -> tuple[bool, str]:
    exchange = build_exchange_stage3(settings, force_dry_run=True)
    try:
        min_notional: Decimal | None = None
        for pair in exchange.get_exchange_info():
            if normalize_symbol(pair.pair_symbol) != symbol:
                continue
            if pair.min_total_amount is None:
                continue
            value = Decimal(str(pair.min_total_amount))
            min_notional = value if min_notional is None else max(min_notional, value)
        if min_notional is not None and requested_notional < min_notional:
            return (
                False,
                f"canary: requested --notional-try={requested_notional} is below exchange minimum "
                f"for {symbol} (min_notional={min_notional})",
            )
        return True, ""
    finally:
        _close_best_effort(exchange, "exchange")


def _run_canary_doctor_gate(
    settings: Settings,
    *,
    db_path: str,
    allow_warn: bool,
) -> tuple[str, int]:
    report = run_health_checks(settings, db_path=db_path, dataset_path=None)
    status = doctor_status(report)
    if status == "fail":
        print("canary: doctor gate FAIL; aborting")
        return status, 2
    if status == "warn" and not allow_warn:
        print("canary: doctor gate WARN; pass --allow-warn to proceed")
        return status, 1
    return status, 0


def _canary_summary_counts(db_path: str, started_at_iso: str) -> dict[str, int]:
    try:
        with sqlite_connection_context(db_path) as conn:
            orders_submitted = int(
                conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE created_at >= ?",
                    (started_at_iso,),
                ).fetchone()[0]
            )
            orders_filled = int(
                conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE updated_at >= ? AND status = ?",
                    (started_at_iso, "FILLED"),
                ).fetchone()[0]
            )
            orders_rejected = int(
                conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE updated_at >= ? AND status = ?",
                    (started_at_iso, "REJECTED"),
                ).fetchone()[0]
            )
            stale_blocks = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(CAST(json_extract(counts_json, '$.blocked_by_market_data') AS INTEGER)), 0)
                    FROM cycle_audit
                    WHERE ts >= ?
                    """,
                    (started_at_iso,),
                ).fetchone()[0]
            )
    except sqlite3.OperationalError:
        return {
            "orders_submitted": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
            "stale_blocks": 0,
        }
    return {
        "orders_submitted": orders_submitted,
        "orders_filled": orders_filled,
        "orders_rejected": orders_rejected,
        "stale_blocks": stale_blocks,
    }


def _print_canary_evidence_commands(export_out: str | None) -> None:
    print("canary: recommended evidence commands:")
    print("  btcbot doctor --json")
    print("  btcbot stage7-report --last 20")
    if export_out:
        print(f"  btcbot stage7-export --last 50 --format jsonl --out {export_out}")
    else:
        print("  btcbot stage7-export --last 50 --format jsonl --out ./stage7-canary.jsonl")


def run_canary(
    settings: Settings,
    *,
    mode: str,
    symbol: str | None,
    notional_try: Decimal,
    cycle_seconds: int,
    max_cycles: int | None,
    ttl_seconds: int,
    db_path: str | None,
    market_data_mode: str | None,
    allow_warn: bool,
    export_out: str | None,
    json_output: bool = False,
) -> int:
    if cycle_seconds < 0 or ttl_seconds <= 0:
        print("canary: cycle-seconds must be >= 0 and ttl-seconds must be > 0")
        return 2
    if notional_try <= 0:
        print("canary: notional-try must be > 0")
        return 2
    if mode == "loop" and (max_cycles is None or max_cycles <= 0):
        print("canary: --max-cycles must be >= 1 in loop mode")
        return 2

    resolved_symbol = _resolve_canary_symbol(settings, symbol)
    if resolved_symbol is None:
        print(
            "canary: --symbol is required when configured UNIVERSE_SYMBOLS contains multiple symbols"
        )
        return 2

    resolved_db_path = (db_path or settings.state_db_path or "").strip()
    if not resolved_db_path:
        print("canary: missing DB path (set STATE_DB_PATH or pass --db-path)")
        return 2

    try:
        with single_instance_lock(db_path=resolved_db_path, account_key="canary"):
            canary_settings = _build_canary_settings(
                settings,
                symbol=resolved_symbol,
                notional_try=notional_try,
                ttl_seconds=ttl_seconds,
                db_path=resolved_db_path,
                market_data_mode=market_data_mode,
            )
            set_base_context(
                process_role=canary_settings.process_role,
                state_db_path=canary_settings.state_db_path,
            )
            _print_effective_side_effects_state(
                canary_settings,
                force_dry_run=False,
                include_safe_mode=True,
            )

            _, arm_policy = _compute_live_policy(
                canary_settings,
                force_dry_run=False,
                include_safe_mode=True,
            )
            if not getattr(arm_policy, "allowed", False):
                print(getattr(arm_policy, "message", LIVE_TRADING_NOT_ARMED_MESSAGE))
                return 2

            min_notional_ok, min_notional_message = _check_canary_min_notional(
                canary_settings,
                resolved_symbol,
                notional_try,
            )
            if not min_notional_ok:
                print(min_notional_message)
                return 2

            final_doctor_status, doctor_rc = _run_canary_doctor_gate(
                canary_settings,
                db_path=resolved_db_path,
                allow_warn=allow_warn,
            )
            if doctor_rc != 0:
                return doctor_rc

            try:
                runtime_state_store = _build_state_store(
                    resolved_db_path,
                    strict_instance_lock=True,
                    settings=canary_settings,
                )
            except TypeError:
                runtime_state_store = _build_state_store(
                    resolved_db_path,
                    strict_instance_lock=True,
                )
            logger.info(
                "state_store_runtime_owner",
                extra={
                    "extra": {
                        "db_path": getattr(
                            runtime_state_store, "db_path_abs", settings.state_db_path
                        ),
                        "instance_id": getattr(runtime_state_store, "instance_id", ""),
                        "strict_lock": True,
                        "heartbeat_interval_seconds": max(1, cycle_seconds),
                    }
                },
            )
            started_at = datetime.now(UTC)
            cycles_run = 0
            rc = 0
            doctor_recheck_every_cycles = 5
            try:
                while True:
                    heartbeat_instance_lock = getattr(runtime_state_store, "heartbeat_instance_lock", None)
                    if callable(heartbeat_instance_lock):
                        heartbeat_instance_lock()
                    rc = run_cycle(
                        canary_settings,
                        force_dry_run=False,
                        state_store=runtime_state_store,
                    )
                    cycles_run += 1
                    if rc != 0:
                        break
                    if mode == "once" or (max_cycles is not None and cycles_run >= max_cycles):
                        break

                    if cycles_run % doctor_recheck_every_cycles == 0:
                        final_doctor_status, doctor_rc = _run_canary_doctor_gate(
                            canary_settings,
                            db_path=resolved_db_path,
                            allow_warn=allow_warn,
                        )
                        if doctor_rc != 0:
                            rc = doctor_rc
                            break

                    sleep_for = cycle_seconds if cycle_seconds > 0 else 1
                    time.sleep(sleep_for)

                    heartbeat_instance_lock = getattr(runtime_state_store, "heartbeat_instance_lock", None)
                    if callable(heartbeat_instance_lock):
                        heartbeat_instance_lock()

                started_at_iso = started_at.isoformat()
                summary = _canary_summary_counts(resolved_db_path, started_at_iso)
                canary_payload = redact_data(
                    {
                        "mode": mode,
                        "cycles": cycles_run,
                        **summary,
                        "final_doctor_status": final_doctor_status.upper(),
                    }
                )
                if json_output:
                    print(json.dumps(canary_payload, sort_keys=True, default=str))
                else:
                    print(
                        "canary summary: "
                        f"mode={mode} cycles={cycles_run} orders_submitted={summary['orders_submitted']} "
                        f"orders_filled={summary['orders_filled']} orders_rejected={summary['orders_rejected']} "
                        f"stale_blocks={summary['stale_blocks']} final_doctor_status={final_doctor_status.upper()}"
                    )

                if export_out:
                    run_stage7_export(
                        canary_settings,
                        db_path=resolved_db_path,
                        last=50,
                        export_format="jsonl",
                        out_path=export_out,
                    )
                    print(f"canary: exported stage7 rows to {export_out}")
                _print_canary_evidence_commands(export_out)
                return rc
            finally:
                release_instance_lock = getattr(runtime_state_store, "release_instance_lock", None)
                if callable(release_instance_lock):
                    release_instance_lock(status="ended")
    except RuntimeError as exc:
        logger.error("canary_lock_acquire_failed", extra={"extra": {"error": str(exc)}})
        print(str(exc))
        return 2


def _apply_effective_universe(settings: Settings) -> Settings:
    if not hasattr(settings, "symbols"):
        return settings
    resolved = resolve_effective_universe(settings)
    logger.info(
        "effective_settings",
        extra={
            "extra": {
                "symbols": resolved.symbols,
                "universe_size": len(resolved.symbols),
                "source": resolved.source,
                "rejected_symbols": resolved.rejected_symbols,
                "suggested_symbols": resolved.suggestions,
                "auto_corrected_symbols": resolved.auto_corrected_symbols,
                "metadata_available": resolved.metadata_available,
            }
        },
    )
    if resolved.rejected_symbols:
        logger.warning(
            "configured symbols rejected by exchange metadata",
            extra={
                "extra": {
                    "rejected_symbols": resolved.rejected_symbols,
                    "suggested_symbols": resolved.suggestions,
                    "auto_corrected_symbols": resolved.auto_corrected_symbols,
                }
            },
        )
    if not hasattr(settings, "model_copy"):
        return settings
    return settings.model_copy(update={"symbols": resolved.symbols})


def _log_arm_check(settings: Settings, *, dry_run: bool) -> None:
    api_key_present = settings.btcturk_api_key is not None
    api_secret_present = settings.btcturk_api_secret is not None
    summary = {
        "dry_run": dry_run,
        "LIVE_TRADING": settings.live_trading,
        "LIVE_TRADING_ACK": settings.live_trading_ack == "I_UNDERSTAND",
        "KILL_SWITCH": settings.kill_switch,
        "api_key_present": api_key_present,
        "api_secret_present": api_secret_present,
        "armed": (
            not dry_run
            and settings.live_trading
            and settings.live_trading_ack == "I_UNDERSTAND"
            and not settings.kill_switch
            and api_key_present
            and api_secret_present
        ),
    }
    logger.info("arm_check", extra={"extra": summary})


def _compute_live_policy(
    settings: Settings,
    *,
    force_dry_run: bool,
    include_safe_mode: bool,
    cycle_id: str | None = None,
) -> tuple[dict[str, bool], object]:
    safe_mode_fn = getattr(settings, "is_safe_mode_enabled", None)
    safe_mode = (
        bool(safe_mode_fn())
        if callable(safe_mode_fn)
        else bool(getattr(settings, "safe_mode", False))
    )
    effective_safe_mode = safe_mode if include_safe_mode else False
    dry_run = bool(force_dry_run or getattr(settings, "dry_run", False) or effective_safe_mode)
    live_ack = getattr(settings, "live_trading_ack", None) == "I_UNDERSTAND"
    inputs = {
        "dry_run": dry_run,
        "kill_switch": bool(getattr(settings, "kill_switch", False) or effective_safe_mode),
        "live_trading_enabled": bool(getattr(settings, "live_trading", False)),
        "live_trading_ack": live_ack,
    }
    policy = validate_live_side_effects_policy(
        **inputs,
        cycle_id=cycle_id,
        logger=logger if cycle_id else None,
        decision_layer="policy_gate",
        action="BLOCK",
        scope="global",
    )
    return inputs, policy


def _format_effective_side_effects_banner(inputs: Mapping[str, bool], policy: object) -> str:
    mode = "ARMED" if getattr(policy, "allowed", False) else "BLOCKED"
    reasons = getattr(policy, "reasons", [])
    reason_text = ",".join(reasons) if reasons else "NONE"
    warning = " | WARNING: Side effects are BLOCKED" if mode == "BLOCKED" else ""
    return (
        f"Effective Side-Effects State: {mode} | "
        f"dry_run={inputs['dry_run']} "
        f"kill_switch={inputs['kill_switch']} "
        f"live_trading_enabled={inputs['live_trading_enabled']} "
        f"ack={inputs['live_trading_ack']} | "
        f"reasons={reason_text}{warning}"
    )


def _print_effective_side_effects_state(
    settings: Settings, *, force_dry_run: bool, include_safe_mode: bool
) -> None:
    inputs, policy = _compute_live_policy(
        settings, force_dry_run=force_dry_run, include_safe_mode=include_safe_mode
    )
    banner = _format_effective_side_effects_banner(inputs, policy)
    print(banner)


def run_cycle(
    settings: Settings,
    force_dry_run: bool = False,
    state_store: StateStore | None = None,
) -> int:
    run_id = uuid4().hex
    cycle_id = uuid4().hex
    inputs, live_policy = _compute_live_policy(
        settings,
        force_dry_run=force_dry_run,
        include_safe_mode=True,
        cycle_id=cycle_id,
    )
    dry_run = inputs["dry_run"]
    effective_safe_mode = settings.is_safe_mode_enabled()
    if effective_safe_mode:
        logger.warning(
            "SAFE_MODE_ENABLED_OBSERVE_ONLY",
            extra={"extra": {"safe_mode": True, "banner": "*** SAFE MODE ACTIVE ***"}},
        )

    _log_arm_check(settings, dry_run=dry_run)
    if not dry_run and not live_policy.allowed:
        logger.error(
            live_policy.message,
            extra={"extra": {"reasons": live_policy.reasons}},
        )
        print(live_policy.message)
        return 2

    exchange = build_exchange_stage3(settings, force_dry_run=dry_run)
    live_armed = bool(
        (not dry_run)
        and bool(getattr(settings, "live_trading", False))
        and (str(getattr(settings, "live_trading_ack", "")) == "I_UNDERSTAND")
        and (not bool(getattr(settings, "kill_switch", False)))
    )
    strict_lock = True if live_armed else bool(getattr(settings, "state_db_strict_lock", True))
    try:
        if state_store is None:
            try:
                resolved_state_store = _build_state_store(
                    settings.state_db_path,
                    strict_instance_lock=strict_lock,
                    settings=settings,
                )
            except TypeError:
                resolved_state_store = _build_state_store(
                    settings.state_db_path,
                    strict_instance_lock=strict_lock,
                )
        else:
            resolved_state_store = state_store
    except RuntimeError as exc:
        if "STATE_DB_LOCK_CONFLICT" in str(exc):
            logger.error("db_instance_lock_conflict", extra={"extra": {"error": str(exc)}})
            print(str(exc))
            return 2
        raise
    try:
        with with_logging_context(run_id=run_id, cycle_id=cycle_id):
            heartbeat_instance_lock = getattr(resolved_state_store, "heartbeat_instance_lock", None)
            if callable(heartbeat_instance_lock):
                heartbeat_instance_lock()
            logger.info(
                "cycle_start",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "mode": "stage3",
                        "dry_run": dry_run,
                        "kill_switch": bool(getattr(settings, "kill_switch", False)),
                        "safe_mode": bool(effective_safe_mode),
                        "live_trading": bool(getattr(settings, "live_trading", False)),
                        "armed": bool((not dry_run) and live_policy.allowed),
                        "db_instance_id": getattr(resolved_state_store, "instance_id", ""),
                    }
                },
            )
            if settings.kill_switch or effective_safe_mode:
                logger.warning(
                    "observe_only_mode_enabled; planning/logging continue "
                    "but write-side effects are blocked"
                )

            instrumentation = get_instrumentation()
            with instrumentation.trace(
                "planning_cycle", attrs={"run_id": run_id, "cycle_id": cycle_id}
            ):
                portfolio_service = PortfolioService(exchange)
                try:
                    market_data_service = MarketDataService(
                        exchange,
                        mode=settings.market_data_mode,
                        ws_rest_fallback=settings.ws_market_data_rest_fallback,
                        orderbook_ttl_ms=settings.orderbook_ttl_ms,
                        orderbook_max_staleness_ms=settings.orderbook_max_staleness_ms,
                    )
                except TypeError:
                    market_data_service = MarketDataService(exchange)
                sweep_service = SweepService(
                    state_store=resolved_state_store,
                    target_try=settings.target_try,
                    offset_bps=settings.offset_bps,
                    default_min_notional=settings.min_order_notional_try,
                )
                execution_service = ExecutionService(
                    exchange=exchange,
                    state_store=resolved_state_store,
                    market_data_service=market_data_service,
                    dry_run=dry_run,
                    ttl_seconds=settings.ttl_seconds,
                    kill_switch=(settings.kill_switch or effective_safe_mode),
                    live_trading_enabled=settings.live_trading,
                    live_trading_ack=settings.live_trading_ack == "I_UNDERSTAND",
                    safe_mode=effective_safe_mode,
                    # Explicit wiring keeps execution-side inventory gating aligned with config;
                    # this policy must be enforced before any exchange I/O in execute_intents.
                    spot_sell_requires_inventory=settings.spot_sell_requires_inventory,
                )
                accounting_service = AccountingService(
                    exchange=exchange, state_store=resolved_state_store
                )
                strategy_service = StrategyService(
                    strategy=ProfitAwareStrategyV1(),
                    settings=settings,
                    market_data_service=market_data_service,
                    accounting_service=accounting_service,
                    state_store=resolved_state_store,
                )
                risk_service = RiskService(
                    risk_policy=RiskPolicy(
                        rules_provider=MarketDataExchangeRulesProvider(
                            market_data_service,
                            allow_default_fallback=dry_run,
                        ),
                        max_orders_per_cycle=settings.max_orders_per_cycle,
                        max_open_orders_per_symbol=settings.max_open_orders_per_symbol,
                        cooldown_seconds=settings.cooldown_seconds,
                        notional_cap_try_per_cycle=Decimal(
                            str(settings.notional_cap_try_per_cycle)
                        ),
                        max_notional_per_order_try=Decimal(
                            str(settings.max_notional_per_order_try)
                        ),
                    ),
                    state_store=resolved_state_store,
                    balance_debug_enabled=settings.risk_balance_debug,
                )

                balances = portfolio_service.get_balances()
                bids, freshness = market_data_service.get_best_bids_with_freshness(
                    settings.symbols,
                    max_age_ms=settings.max_market_data_age_ms,
                )
                if freshness is not None and bool(getattr(freshness, "is_stale", False)):
                    observed_age_ms = getattr(freshness, "observed_age_ms", None)
                    max_age_ms = int(
                        getattr(freshness, "max_age_ms", settings.max_market_data_age_ms)
                    )
                    source_mode = str(getattr(freshness, "source_mode", settings.market_data_mode))
                    connected = bool(getattr(freshness, "connected", False))
                    missing_symbols = list(getattr(freshness, "missing_symbols", ()))
                    envelope = {
                        "cycle_id": cycle_id,
                        "run_id": run_id,
                        "decision_layer": "market_data",
                        "reason_code": "market_data:stale",
                        "action": "BLOCK",
                        "scope": "global",
                        "observed_age_ms": observed_age_ms,
                        "max_age_ms": max_age_ms,
                        "symbols": [normalize_symbol(symbol) for symbol in settings.symbols],
                        "market_data_mode": source_mode,
                        "ws_connected": connected,
                        "missing_symbols": missing_symbols,
                    }
                    emit_decision(logger, envelope)
                    resolved_state_store.record_cycle_audit(
                        cycle_id=cycle_id,
                        counts={"blocked_by_market_data": 1},
                        decisions=["market_data:stale"],
                        envelope=envelope,
                    )
                    logger.warning(
                        "market_data_stale_fail_closed",
                        extra={"extra": envelope},
                    )
                    resolved_state_store.set_last_cycle_id(cycle_id)
                    return 0

                startup_mark_prices = {
                    normalize_symbol(symbol): Decimal(str(price))
                    for symbol, price in bids.items()
                    if price > 0
                }

                recovery = StartupRecoveryService().run(
                    cycle_id=cycle_id,
                    symbols=settings.symbols,
                    execution_service=execution_service,
                    accounting_service=accounting_service,
                    portfolio_service=portfolio_service,
                    mark_prices=startup_mark_prices,
                    do_refresh_lifecycle=False,
                    state_store=resolved_state_store,
                )
                if recovery.observe_only_required:
                    logger.error(
                        "startup_recovery_forced_observe_only",
                        extra={
                            "extra": {
                                "invariant_errors": list(recovery.invariant_errors),
                                "observe_only_reason": recovery.observe_only_reason,
                            }
                        },
                    )
                    execution_service.kill_switch = True

                lifecycle_summary: dict[str, object] = {}
                lifecycle_backoff_threshold = 3
                backoff_degraded_observe_only = False

                reconcile_started = time.monotonic()
                execution_service.cancel_stale_orders(cycle_id=cycle_id)
                instrumentation.histogram(
                    "reconcile_lag_ms",
                    (time.monotonic() - reconcile_started) * 1000,
                    attrs={"cycle_id": cycle_id},
                )

                mark_prices = {
                    normalize_symbol(symbol): Decimal(str(price))
                    for symbol, price in bids.items()
                    if price > 0
                }
                stale_count = len([symbol for symbol, price in bids.items() if price <= 0])
                if settings.symbols:
                    instrumentation.counter(
                        "invalid_best_bid_count",
                        stale_count,
                        attrs={"cycle_id": cycle_id},
                    )
                    # Deprecated compatibility metric; retains prior behavior
                    # (count of non-positive best bids) under the old name.
                    instrumentation.counter(
                        "stale_market_data_rate",
                        stale_count,
                        attrs={"cycle_id": cycle_id},
                    )

                fills_inserted = accounting_service.refresh(settings.symbols, mark_prices)
                try_cash_target = Decimal(str(settings.try_cash_target))
                account_snapshot = build_cycle_account_snapshot(
                    balances,
                    try_cash_target=try_cash_target,
                    now_utc=datetime.now(UTC),
                    quote_asset=getattr(settings, "universe_quote_currency", "TRY"),
                )
                cash_try_free = account_snapshot.cash_try_free
                investable_try = account_snapshot.investable_try
                try_cash_target = account_snapshot.try_cash_target
                raw_intents = strategy_service.generate(
                    cycle_id=cycle_id, symbols=settings.symbols, balances=balances
                )
                refresh_order_lifecycle = getattr(
                    execution_service, "refresh_order_lifecycle", None
                )
                scoped_symbols = {
                    normalize_symbol(getattr(intent, "symbol", ""))
                    for intent in raw_intents
                    if getattr(intent, "symbol", "")
                }
                local_active_orders = []
                find_open_or_unknown_orders = getattr(
                    resolved_state_store,
                    "find_open_or_unknown_orders",
                    None,
                )
                if callable(find_open_or_unknown_orders):
                    try:
                        local_active_orders = find_open_or_unknown_orders(
                            settings.symbols,
                            new_grace_seconds=PENDING_GRACE_SECONDS,
                            include_new_after_grace=False,
                            include_escalated_unknown=False,
                        )
                    except TypeError:
                        local_active_orders = find_open_or_unknown_orders()
                    for order in local_active_orders:
                        scoped_symbols.add(normalize_symbol(getattr(order, "symbol", "")))
                scoped_symbols = sorted(symbol for symbol in scoped_symbols if symbol)
                if callable(refresh_order_lifecycle) and scoped_symbols:
                    lifecycle_summary = refresh_order_lifecycle(scoped_symbols)
                    mark_lifecycle_refreshed = getattr(
                        execution_service, "mark_lifecycle_refreshed", None
                    )
                    if callable(mark_lifecycle_refreshed):
                        mark_lifecycle_refreshed(cycle_id=cycle_id)

                backoff_count = int(lifecycle_summary.get("backoff_429_count", 0) or 0)
                breaker_open = bool(lifecycle_summary.get("error_code") == "EXCHANGE_429_BACKOFF")
                if backoff_count >= lifecycle_backoff_threshold or breaker_open:
                    backoff_degraded_observe_only = True
                    if hasattr(execution_service, "kill_switch"):
                        execution_service.kill_switch = True
                    emit_decision(
                        logger,
                        {
                            "cycle_id": cycle_id,
                            "decision_layer": "execution",
                            "reason_code": "EXCHANGE_429_BACKOFF",
                            "action": "SUPPRESS",
                            "scope": "global",
                            "backoff_429_count": backoff_count,
                            "impacted_endpoints": list(
                                lifecycle_summary.get("backoff_endpoints", [])
                            ),
                        },
                    )

                approved_intents = risk_service.filter(
                    cycle_id=cycle_id,
                    intents=raw_intents,
                    try_cash_target=try_cash_target,
                    investable_try=investable_try,
                    account_snapshot=account_snapshot,
                )

                _ = sweep_service.build_order_intents(
                    cycle_id=cycle_id,
                    balances=balances,
                    symbols=settings.symbols,
                    best_bids=bids,
                )

                submit_started = time.monotonic()
                placed = (
                    0
                    if backoff_degraded_observe_only
                    else execution_service.execute_intents(approved_intents, cycle_id=cycle_id)
                )
                instrumentation.histogram(
                    "order_submit_latency_ms",
                    (time.monotonic() - submit_started) * 1000,
                    attrs={"cycle_id": cycle_id},
                )
                instrumentation.gauge(
                    "circuit_breaker_state",
                    1.0 if bool(getattr(execution_service, "kill_switch", False)) else 0.0,
                    attrs={"cycle_id": cycle_id},
                )

                planned_spend_try = Decimal("0")
                for intent in approved_intents:
                    qty = getattr(intent, "qty", None)
                    limit_price = getattr(intent, "limit_price", None)
                    if qty is not None and limit_price is not None:
                        planned_spend_try += Decimal(str(qty)) * Decimal(str(limit_price))
                resolved_state_store.set_last_cycle_id(cycle_id)
                blocked_by_gate = (
                    len(approved_intents)
                    if (
                        settings.kill_switch or effective_safe_mode or backoff_degraded_observe_only
                    )
                    else 0
                )
                suppressed_dry_run = len(approved_intents) if dry_run else 0
                rejected_by_risk = max(0, len(raw_intents) - len(approved_intents))
                execution_summary = dict(
                    getattr(execution_service, "last_execute_summary", {}) or {}
                )
                rejected_local = int(execution_summary.get("rejected_intents", 0))
                rejected_precheck = int(execution_summary.get("intents_rejected_precheck", 0))
                failed_exchange = int(execution_summary.get("orders_failed_exchange", 0))
                attempted_exchange_calls = int(execution_summary.get("attempted_exchange_calls", 0))
                if failed_exchange > attempted_exchange_calls:
                    failed_exchange = attempted_exchange_calls
                rejected_total = rejected_by_risk + rejected_local
                blocked_events = list(
                    getattr(getattr(risk_service, "risk_policy", None), "last_blocked_events", [])
                )
                reject_counts: dict[str, int] = {}
                for event in blocked_events:
                    code = str(event.get("error_code") or event.get("reason_code") or "")
                    if not code:
                        continue
                    reject_counts[code] = reject_counts.get(code, 0) + 1
                lifecycle_error_code = str(lifecycle_summary.get("error_code") or "")
                if lifecycle_error_code:
                    reject_counts[lifecycle_error_code] = (
                        reject_counts.get(lifecycle_error_code, 0) + 1
                    )
                top_reject_reasons = [
                    code
                    for code, _ in sorted(
                        reject_counts.items(), key=lambda item: item[1], reverse=True
                    )
                ][:3]
                max_open_block = next(
                    (
                        event
                        for event in blocked_events
                        if str(event.get("error_code")) == "RISK_BLOCK_MAX_OPEN_ORDERS"
                    ),
                    None,
                )
                mode_final = (
                    "OBSERVE_ONLY"
                    if (
                        settings.kill_switch or effective_safe_mode or backoff_degraded_observe_only
                    )
                    else "ARMED"
                )
                logger.info(
                    "Cycle completed",
                    extra={
                        "extra": {
                            "cycle_event": "cycle_end",
                            "cycle_id": cycle_id,
                            "cash_try_free": str(cash_try_free),
                            "try_cash_target": str(try_cash_target),
                            "investable_try": str(investable_try),
                            "notional_cap_try_per_cycle": str(settings.notional_cap_try_per_cycle),
                            "planned_spend_try": str(planned_spend_try),
                            "raw_intents": len(raw_intents),
                            "approved_intents": len(approved_intents),
                            "orders_submitted": placed,
                            "orders_blocked_by_gate": blocked_by_gate,
                            "orders_suppressed_dry_run": suppressed_dry_run,
                            "orders_simulated": suppressed_dry_run,
                            "rejected_intents": rejected_total,
                            "intents_rejected_precheck": rejected_precheck,
                            "top_reject_reasons": top_reject_reasons,
                            "orders_failed_exchange": failed_exchange,
                            "attempted_exchange_calls": attempted_exchange_calls,
                            "fills_inserted": fills_inserted,
                            "positions": len(accounting_service.get_positions()),
                            "dry_run": dry_run,
                            "kill_switch": bool(
                                settings.kill_switch
                                or effective_safe_mode
                                or backoff_degraded_observe_only
                            ),
                            "safe_mode": effective_safe_mode,
                            "mode_final": mode_final,
                            "lifecycle_backoff_429_count": backoff_count,
                            "lifecycle_impacted_endpoints": list(
                                lifecycle_summary.get("backoff_endpoints", [])
                            ),
                            "open_orders_count_origin": (max_open_block or {}).get(
                                "open_orders_count_origin"
                            ),
                            "open_order_identifiers": (max_open_block or {}).get(
                                "open_order_identifiers", []
                            ),
                        }
                    },
                )
        return 0
    except ConfigurationError as exc:
        logger.exception(
            "Cycle failed due to configuration error",
            extra={
                "extra": {
                    "error_type": type(exc).__name__,
                    "safe_message": str(exc),
                }
            },
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Cycle failed",
            extra={
                "extra": {
                    "error_type": type(exc).__name__,
                    "safe_message": str(exc),
                }
            },
        )
        return 1
    finally:
        flush_instrumentation()
        _flush_logging_handlers()
        _close_best_effort(exchange, "exchange")



def run_stage4_freeze_status(*, settings: Settings, db_path: str | None = None) -> int:
    resolved_db = normalize_db_path(db_path or settings.state_db_path)
    store = StateStore(str(resolved_db))
    process_role = _canonical_process_role(settings)
    freeze = store.stage4_get_freeze(process_role)
    payload = {
        "process_role": process_role,
        "active": freeze.active,
        "reason": freeze.reason,
        "since": freeze.since_ts,
        "last_seen": freeze.last_seen_ts,
        "details": freeze.details,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def run_stage4_freeze_clear(
    *,
    settings: Settings,
    db_path: str | None = None,
    confirmed: bool = False,
) -> int:
    if not confirmed:
        print("Refusing to clear freeze state without --yes")
        return 2
    resolved_db = normalize_db_path(db_path or settings.state_db_path)
    store = StateStore(str(resolved_db))
    process_role = _canonical_process_role(settings)
    previous = store.stage4_get_freeze(process_role)
    store.stage4_clear_freeze(process_role)
    duration_seconds = None
    if previous.since_ts:
        try:
            since = datetime.fromisoformat(previous.since_ts)
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            duration_seconds = int((datetime.now(UTC) - since.astimezone(UTC)).total_seconds())
        except ValueError:
            duration_seconds = None
    logger.info(
        "stage4_unknown_freeze_clear",
        extra={
            "extra": {
                "process_role": process_role,
                "previous_reason": previous.reason,
                "duration_seconds": duration_seconds,
            }
        },
    )
    print(json.dumps({"process_role": process_role, "cleared": True, "previous_reason": previous.reason}, sort_keys=True))
    return 0

def _degrade_status_payload(store: StateStore) -> dict[str, object]:
    state = store.get_degrade_state_current()
    recent_codes = store.fetch_recent_anomaly_codes(limit=10)
    return {
        "degrade_state_current": state,
        "last_anomaly_codes": recent_codes,
    }


def run_degrade_status(*, settings: Settings, db_path: str | None = None) -> int:
    resolved_db = normalize_db_path(db_path or settings.state_db_path)
    store = StateStore(str(resolved_db))
    print(json.dumps(_degrade_status_payload(store), sort_keys=True))
    return 0


def run_degrade_clear(*, settings: Settings, db_path: str | None = None) -> int:
    resolved_db = normalize_db_path(db_path or settings.state_db_path)
    store = StateStore(str(resolved_db))
    store.upsert_degrade_state_current(
        cooldown_until=None,
        current_override_mode=None,
        last_reasons_json=json.dumps({"level": 0, "reasons": []}, sort_keys=True),
        warn_window_count=0,
        last_warn_codes_json="[]",
        cursor_stall_cycles_json="{}",
        last_reject_count=0,
    )
    print(json.dumps({"cleared": True}, sort_keys=True))
    return 0


def run_degrade_override(
    *,
    settings: Settings,
    db_path: str | None = None,
    mode: str,
    cooldown_seconds: int,
    reason: str,
) -> int:
    resolved_db = normalize_db_path(db_path or settings.state_db_path)
    store = StateStore(str(resolved_db))
    now = datetime.now(UTC)
    normalized_mode = None if mode == "NORMAL" else mode
    cooldown_until = None
    if normalized_mode is not None:
        cooldown_until = (now + timedelta(seconds=max(0, int(cooldown_seconds)))).isoformat()
    payload = {
        "level": 0 if normalized_mode is None else (3 if normalized_mode == "OBSERVE_ONLY" else 2),
        "override_mode": normalized_mode,
        "reasons": ["MANUAL_OVERRIDE", reason],
        "recovery_streak": 0,
    }
    store.persist_degrade(
        cycle_id=f"manual_override:{now.isoformat()}",
        events=[
            AnomalyEvent(
                code=AnomalyCode.MANUAL_OVERRIDE,
                severity="WARN",
                ts=now,
                details={"reason": reason, "mode": mode},
            )
        ],
        cooldown_until=cooldown_until,
        current_override_mode=normalized_mode,
        last_reasons_json=json.dumps(payload, sort_keys=True),
        warn_window_count=0,
        last_warn_codes_json=json.dumps([AnomalyCode.MANUAL_OVERRIDE.value]),
        cursor_stall_cycles_json="{}",
        last_reject_count=0,
    )
    print(
        json.dumps(
            {
                "override_mode": normalized_mode,
                "cooldown_until": cooldown_until,
                "reason": reason,
            },
            sort_keys=True,
        )
    )
    return 0




_FORCE_UNLOCK_ACK = "I_UNDERSTAND_STATE_DB_UNLOCK"


def _pid_appears_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_state_db_locks_list(*, settings: Settings, db_path: str | None = None) -> int:
    resolved_db = normalize_db_path(db_path or settings.state_db_path)
    store = StateStore(str(resolved_db))
    now_epoch = int(datetime.now(UTC).timestamp())
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT instance_id, pid, db_path, started_at_epoch, heartbeat_at_epoch, status, ended_at_epoch
            FROM process_instances
            ORDER BY heartbeat_at_epoch DESC
            """
        ).fetchall()
    payload_rows: list[dict[str, object]] = []
    for row in rows:
        heartbeat_age_seconds = max(0, now_epoch - int(row["heartbeat_at_epoch"]))
        payload_rows.append(
            {
                "instance_id": str(row["instance_id"]),
                "pid": int(row["pid"]),
                "status": str(row["status"]),
                "heartbeat_age_seconds": heartbeat_age_seconds,
                "strict_instance_lock": bool(store.strict_instance_lock),
                "pid_appears_alive": _pid_appears_alive(int(row["pid"])),
            }
        )
    print(json.dumps({"db_path": str(resolved_db), "locks": payload_rows}, sort_keys=True))
    return 0


def run_state_db_unlock(
    *,
    settings: Settings,
    db_path: str | None = None,
    instance_id: str,
    force: bool,
    force_ack: str | None,
) -> int:
    resolved_db = normalize_db_path(db_path or settings.state_db_path)
    store = StateStore(str(resolved_db))
    now_epoch = int(datetime.now(UTC).timestamp())
    ttl_seconds = int(store.process_instance_ttl_seconds)
    with store._connect() as conn:
        row = conn.execute(
            """
            SELECT instance_id, pid, heartbeat_at_epoch, status
            FROM process_instances
            WHERE instance_id=?
            """,
            (instance_id,),
        ).fetchone()
        if row is None:
            print(f"No process lock instance found for instance_id={instance_id}")
            return 2
        pid = int(row["pid"])
        heartbeat_age_seconds = max(0, now_epoch - int(row["heartbeat_at_epoch"]))
        heartbeat_fresh = heartbeat_age_seconds <= ttl_seconds
        pid_alive = _pid_appears_alive(pid)
        appears_active = str(row["status"]) == "active" and (heartbeat_fresh or pid_alive)
        if appears_active and not force:
            print(
                "Refusing to unlock active instance. Re-run with --force "
                f"--force-ack={_FORCE_UNLOCK_ACK} only if you are sure the process is dead."
            )
            return 2
        if force and force_ack != _FORCE_UNLOCK_ACK:
            print(f"--force requires --force-ack={_FORCE_UNLOCK_ACK}")
            return 2

        conn.execute(
            """
            UPDATE process_instances
            SET status='force_unlocked', ended_at_epoch=?, heartbeat_at_epoch=?
            WHERE instance_id=?
            """,
            (now_epoch, now_epoch, instance_id),
        )
    logger.warning(
        "state_db_unlock_audit",
        extra={
            "extra": {
                "instance_id": instance_id,
                "db_path": str(resolved_db),
                "forced": bool(force),
                "force_ack": bool(force),
            }
        },
    )
    print(json.dumps({"unlocked": True, "instance_id": instance_id, "forced": bool(force)}, sort_keys=True))
    return 0

def run_cycle_stage4(
    settings: Settings, force_dry_run: bool = False, db_path: str | None = None
) -> int:
    inputs, live_policy = _compute_live_policy(
        settings, force_dry_run=force_dry_run, include_safe_mode=True
    )
    dry_run = inputs["dry_run"]
    if settings.is_safe_mode_enabled():
        logger.warning(
            "SAFE_MODE_ENABLED_OBSERVE_ONLY",
            extra={"extra": {"safe_mode": True, "banner": "*** SAFE MODE ACTIVE ***"}},
        )
    _log_arm_check(settings, dry_run=dry_run)
    resolved_db_path = _resolve_stage7_db_path(
        "stage4-run", db_path=db_path, settings_db_path=settings.state_db_path
    )
    if resolved_db_path is None:
        return 2
    cycle_runner = Stage4CycleRunner()
    effective_settings = settings.model_copy(
        update={"dry_run": dry_run, "state_db_path": resolved_db_path}
    )
    cycle_id = uuid4().hex
    if not dry_run and not live_policy.allowed:
        logger.error(live_policy.message, extra={"extra": {"reasons": live_policy.reasons}})
        print(live_policy.message)
        StateStore(db_path=resolved_db_path).record_cycle_audit(
            cycle_id=cycle_id,
            counts={"blocked_by_policy": 1},
            decisions=[f"policy_block:{reason.lower()}" for reason in live_policy.reasons],
            envelope={
                "cycle_id": cycle_id,
                "command": "stage4-run",
                "dry_run": dry_run,
                "live_mode": False,
                "symbols": sorted(normalize_symbol(symbol) for symbol in settings.symbols),
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "live_trading_enabled": settings.is_live_trading_enabled(),
                "kill_switch": settings.kill_switch,
            },
        )
        return 2

    try:
        with single_instance_lock(db_path=resolved_db_path, account_key="stage4"):
            logger.info("Running Stage 4 cycle")
            result = cycle_runner.run_one_cycle(effective_settings)
            return result
    except Stage4ConfigurationError as exc:
        logger.exception(
            "Stage 4 cycle failed due to configuration error",
            extra={"extra": {"error_type": type(exc).__name__, "safe_message": str(exc)}},
        )
        return 2
    except Stage4InvariantError as exc:
        logger.exception(
            "Stage 4 cycle failed due to capital/invariant policy",
            extra={
                "extra": {
                    "error_type": type(exc).__name__,
                    "error_category": "capital_invariant",
                    "safe_message": str(exc),
                }
            },
        )
        return 1
    except Stage4ExchangeError as exc:
        logger.exception(
            "Stage 4 cycle failed",
            extra={"extra": {"error_type": type(exc).__name__, "safe_message": str(exc)}},
        )
        return 1


def run_cycle_stage7(
    settings: Settings,
    force_dry_run: bool = False,
    include_adaptation: bool = True,
    db_path: str | None = None,
) -> int:
    dry_run = force_dry_run or settings.dry_run
    if not dry_run:
        print("stage7-run requires --dry-run")
        return 2
    if not settings.stage7_enabled:
        print("stage7-run is disabled; set STAGE7_ENABLED=true to run")
        logger.warning("stage7_disabled_in_settings")
        return 2
    resolved_db_path = _resolve_stage7_db_path(
        "stage7-run", db_path=db_path, settings_db_path=settings.state_db_path
    )
    if resolved_db_path is None:
        return 2
    runner = Stage7CycleRunner()
    effective_settings = settings.model_copy(
        update={"dry_run": True, "state_db_path": resolved_db_path}
    )
    try:
        with single_instance_lock(db_path=resolved_db_path, account_key="stage7"):
            return runner.run_one_cycle(
                effective_settings,
                enable_adaptation=include_adaptation,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Stage 7 cycle failed",
            extra={"extra": {"error_type": type(exc).__name__, "safe_message": str(exc)}},
        )
        return 1


def run_preflight(*, settings: Settings, db_path: str | None = None, profile: str = "live") -> int:
    summary = run_preflight_checks(settings=settings, db_path=db_path, profile=profile)
    passed = bool(summary.get("passed", False))
    status = "PASS" if passed else "FAIL"
    print(f"Preflight ({summary.get('profile')}): {status}")
    for check in summary.get("checks", []):
        marker = "OK" if check.get("ok") else "FAIL"
        print(f" - {check.get('name')}: {marker} ({check.get('detail')})")
    print(json.dumps(summary, sort_keys=True))
    return 0 if passed else 2


def run_health(settings: Settings) -> int:
    with single_instance_lock(db_path=settings.state_db_path, account_key="health"):
        StateStore(db_path=settings.state_db_path)

    client = BtcturkHttpClient(
        api_key=settings.btcturk_api_key.get_secret_value() if settings.btcturk_api_key else None,
        api_secret=settings.btcturk_api_secret.get_secret_value()
        if settings.btcturk_api_secret
        else None,
        base_url=settings.btcturk_base_url,
    )
    try:
        ok = client.health_check()
        status = "OK" if ok else "FAIL"
        print("Configuration: OK")
        print(f"State DB: OK ({settings.state_db_path})")
        print(f"BTCTurk public API health: {status}")
        _print_effective_risk_config(settings)
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Health check could not reach BTCTurk public API",
            extra={"extra": {"error_type": type(exc).__name__}},
        )
        print("Configuration: OK")
        print(f"State DB: OK ({settings.state_db_path})")
        print("BTCTurk public API health: SKIP (unreachable in current environment)")
        _print_effective_risk_config(settings)
        return 0 if settings.dry_run else 1
    finally:
        _close_best_effort(client, "health client")


def _flush_logging_handlers() -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return

    root = logging.getLogger()
    for handler in root.handlers:
        try:
            handler.flush()
        except (OSError, ValueError):
            continue
        except Exception:  # noqa: BLE001
            logger.warning("Failed to flush logging handler", exc_info=True)


def _close_best_effort(resource: object, label: str) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to close resource", extra={"extra": {"resource": label}}, exc_info=True
        )


def _print_effective_risk_config(settings: Settings) -> None:
    print("Effective risk config:")
    print(f"  TRY_CASH_TARGET={settings.try_cash_target}")
    print(f"  NOTIONAL_CAP_TRY_PER_CYCLE={settings.notional_cap_try_per_cycle}")
    print(f"  MAX_NOTIONAL_PER_ORDER_TRY={settings.max_notional_per_order_try}")
    print(f"  MIN_ORDER_NOTIONAL_TRY={settings.min_order_notional_try}")
    print(f"  MAX_ORDERS_PER_CYCLE={settings.max_orders_per_cycle}")
    print(f"  MAX_OPEN_ORDERS_PER_SYMBOL={settings.max_open_orders_per_symbol}")
    print(f"  COOLDOWN_SECONDS={settings.cooldown_seconds}")


def _normalize_flag_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes", "y"}:
            return True
        if token in {"false", "0", "no", "n", ""}:
            return False
    return False


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                return int(text)
            except ValueError:
                return default
    return default


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _as_list_of_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _csv_safe_value(value: object) -> object:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _resolve_stage7_db_path(
    command: str,
    *,
    db_path: str | None,
    settings_db_path: str | None = None,
    silent: bool = False,
) -> str | None:
    candidate = db_path.strip() if db_path and db_path.strip() else None
    if candidate is None and os.getenv("PYTEST_CURRENT_TEST"):
        if settings_db_path and settings_db_path.strip():
            candidate = settings_db_path.strip()
    if candidate is None:
        env_db = os.getenv("STATE_DB_PATH")
        candidate = env_db.strip() if env_db and env_db.strip() else None
    if candidate is None and settings_db_path and settings_db_path.strip():
        candidate = settings_db_path.strip()
    if candidate is not None:
        return candidate

    if not silent:
        print(f"{command}: missing database path.")
        print("Provide --db <path> or set STATE_DB_PATH.")
        print(f"Example: btcbot {command} --db ./btcbot_state.db")
    return None


def run_stage7_report(
    settings: Settings,
    db_path: str | None,
    last: int,
    *,
    json_output: bool = False,
    json_only: bool = False,
) -> int:
    resolved_db_path = _resolve_stage7_db_path(
        "stage7-report", db_path=db_path, settings_db_path=settings.state_db_path
    )
    if resolved_db_path is None:
        return 2

    with single_instance_lock(db_path=resolved_db_path, account_key="stage7-report"):
        store = StateStore(db_path=resolved_db_path)
        cycle_rows = build_cycle_rows(store=store, limit=last)
        validations = validate_cycle_rows(cycle_rows)
        daily = build_stage7_rollup(cycle_rows, "daily")
        weekly = build_stage7_rollup(cycle_rows, "weekly")
        report_obj = {
            "cycles": cycle_rows,
            "rollups": {"daily": daily, "weekly": weekly},
            "validations": validations,
        }
        errors = [finding for finding in validations if finding.severity == "error"]

        only_json = bool(json_only or json_output)
        if not only_json:
            print(
                f"stage7-report: cycles={len(cycle_rows)} daily_buckets={len(daily.buckets)} "
                f"weekly_buckets={len(weekly.buckets)} validation_errors={len(errors)}"
            )

        print(render_stage7_json(redact_data(report_obj)))

        if errors:
            print("stage7-report: FAIL_CLOSED financial validation errors detected", file=sys.stderr)
            return 1

        if only_json:
            return 0

        rows = store.fetch_stage7_run_metrics(limit=last, order_desc=True)
        ledger_metrics = store.get_latest_stage7_ledger_metrics()
        drawdown_ratio = (
            normalize_drawdown_ratio(
                ledger_metrics.get("max_drawdown_ratio") if ledger_metrics is not None else None,
                ledger_metrics.get("max_drawdown_pct") if ledger_metrics is not None else None,
            )
            if ledger_metrics is not None
            else None
        )
        enriched_rows: list[dict[str, object]] = []
        for row in rows:
            cycle_status, _, _ = evaluate_slo_status_for_rows(
                settings,
                [row],
                drawdown_ratio=normalize_drawdown_ratio(
                    row.get("max_drawdown_ratio"),
                    row.get("max_drawdown_pct"),
                ),
            )
            enriched_rows.append({**row, "slo_status": cycle_status})

        window_status, _, window_notes = evaluate_slo_status_for_rows(
            settings,
            rows,
            drawdown_ratio=drawdown_ratio,
        )

        print(
            "cycle_id ts mode net_pnl_try max_dd turnover intents rejects throttled no_trades_reason slo_status"
        )
        for row in enriched_rows:
            no_trades_reason = row.get("no_trades_reason") or "-"
            no_metrics_reason = row.get("no_metrics_reason") or "-"
            print(
                f"{row['cycle_id']} {row['ts']} {row['mode_final']} "
                f"{row['net_pnl_try']} {row['max_drawdown_pct']} {row['turnover_try']} "
                f"{row['intents_planned_count']} {row['oms_rejected_count']} "
                f"{_as_int(row.get('oms_throttled_count', 0))} {no_trades_reason} {row['slo_status'].upper()}"
            )
            print(f"  no_metrics_reason={no_metrics_reason}")

            cycle_trace = store.get_stage7_cycle_trace(str(row["cycle_id"]))
            if cycle_trace is not None:
                summary = _as_mapping(cycle_trace.get("intents_summary", {}))
                portfolio_plan = _as_mapping(cycle_trace.get("portfolio_plan", {}))
                print(
                    "  stage7_plan_summary="
                    f"planned={summary.get('order_intents_planned', 0)} "
                    f"skipped={summary.get('order_intents_skipped', 0)} "
                    f"actions={summary.get('order_decisions_total', 0)}"
                )
                planning_diag = _as_mapping(summary.get("planning_diagnostics"))
                if planning_diag:
                    print(
                        "  planning_diagnostics="
                        f"enabled={planning_diag.get('planning_enabled')} "
                        f"disabled_reason={planning_diag.get('planning_disabled_reason') or '-'} "
                        f"universe={planning_diag.get('selected_universe_count', 0)} "
                        f"mark_prices={planning_diag.get('mark_prices_count', 0)} "
                        f"planned={planning_diag.get('planned_intents_count', 0)} "
                        f"skipped={planning_diag.get('skipped_intents_count', 0)}"
                    )
                    skip_reasons = _as_mapping(planning_diag.get("skip_reasons"))
                    if skip_reasons:
                        reason_items = ", ".join(
                            f"{key}:{value}" for key, value in sorted(skip_reasons.items())
                        )
                        print(f"  planning_skip_reasons={reason_items}")
                if portfolio_plan:
                    print(
                        "  portfolio_plan="
                        f"cash_target_try={portfolio_plan.get('cash_target_try', '-')} "
                        f"actions={len(_as_list_of_mappings(portfolio_plan.get('actions', [])))}"
                    )

            allocation_plan = store.get_allocation_plan(str(row["cycle_id"]))
            plan_source = "cycle_id"
            if allocation_plan is None:
                allocation_plan = store.get_latest_allocation_plan()
                plan_source = "latest"
            if allocation_plan is not None:
                plan_items = _as_list_of_mappings(allocation_plan.get("plan") or [])
                deferred_items = _as_list_of_mappings(allocation_plan.get("deferred") or [])
                investable_total = allocation_plan.get(
                    "investable_total_try"
                ) or allocation_plan.get("investable_try")
                unused_budget = allocation_plan.get("unused_budget_try") or allocation_plan.get(
                    "unused_investable_try"
                )
                print(
                    "  allocation_plan="
                    f"source={plan_source} "
                    f"cycle_id={allocation_plan.get('cycle_id')} "
                    f"investable_total_try={investable_total} "
                    f"investable_this_cycle_try={allocation_plan.get('investable_this_cycle_try')} "
                    f"deploy_budget_try={allocation_plan.get('deploy_budget_try')} "
                    f"planned_total_try={allocation_plan.get('planned_total_try')} "
                    f"unused_budget_try={unused_budget} "
                    f"usage_reason={allocation_plan.get('usage_reason')}"
                )
                print(
                    "  stage4_plan_summary="
                    f"planned_total_try={allocation_plan.get('planned_total_try')} "
                    f"unused_budget_try={unused_budget} "
                    f"actions={len(plan_items)} deferred={len(deferred_items)}"
                )
                if plan_items:
                    selected = ", ".join(
                        f"{item.get('symbol')}:{item.get('notional_try', '-')}"
                        for item in plan_items[:5]
                    )
                    print(f"  selected_symbols={selected}")
                if deferred_items:
                    deferred = ", ".join(
                        f"{item.get('symbol')}:{item.get('reason', 'deferred')}"
                        for item in deferred_items[:5]
                    )
                    print(f"  deferred_symbols={deferred}")
                if no_trades_reason in {"-", None, ""} and plan_items:
                    print("  no_trades_reason=NOT_ARMED")

        print(f"stage7-report: window_status={window_status.upper()} rows={len(rows)}")
        return 0


def run_stage7_export(
    settings: Settings,
    db_path: str | None,
    last: int,
    export_format: str,
    out_path: str | None,
) -> int:
    resolved_db_path = _resolve_stage7_db_path(
        "stage7-export", db_path=db_path, settings_db_path=settings.state_db_path
    )
    if resolved_db_path is None:
        return 2
    with single_instance_lock(db_path=resolved_db_path, account_key="stage7-export"):
        store = StateStore(db_path=resolved_db_path)
        cycle_rows = build_cycle_rows(store=store, limit=last)
    validations = validate_cycle_rows(cycle_rows)
    if any(finding.severity == "error" for finding in validations):
        print("stage7-export: FAIL_CLOSED financial validation errors detected", file=sys.stderr)
        return 1

    daily = build_stage7_rollup(cycle_rows, "daily")
    weekly = build_stage7_rollup(cycle_rows, "weekly")
    report_obj = {
        "cycles": cycle_rows,
        "rollups": {"daily": daily, "weekly": weekly},
        "validations": validations,
    }

    normalized_format = "json" if export_format == "jsonl" else export_format
    csv_text = render_stage7_csv(cycle_rows)
    json_text = render_stage7_json(redact_data(report_obj))

    if out_path is None:
        if normalized_format in {"csv", "both"}:
            print(csv_text.rstrip("\n"))
        if normalized_format in {"json", "both"}:
            print(json_text)
        return 0

    out = Path(out_path)
    if normalized_format == "csv":
        out.write_text(csv_text, encoding="utf-8")
    elif normalized_format == "json":
        out.write_text(json_text + "\n", encoding="utf-8")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".csv").write_text(csv_text, encoding="utf-8")
        out.with_suffix(".json").write_text(json_text + "\n", encoding="utf-8")
    return 0


def run_stage7_alerts(settings: Settings, db_path: str | None, last: int) -> int:
    resolved_db_path = _resolve_stage7_db_path(
        "stage7-alerts", db_path=db_path, settings_db_path=settings.state_db_path
    )
    if resolved_db_path is None:
        return 2
    with single_instance_lock(db_path=resolved_db_path, account_key="stage7-alerts"):
        store = StateStore(db_path=resolved_db_path)
        rows = store.fetch_stage7_run_metrics(limit=last, order_desc=True)
    print("cycle_id ts alerts")
    for row in rows:
        alerts = _as_mapping(row.get("alert_flags", {}))
        normalized_alerts = {name: _normalize_flag_bool(value) for name, value in alerts.items()}
        if any(normalized_alerts.values()):
            active = ",".join(sorted(name for name, value in normalized_alerts.items() if value))
            print(f"{row['cycle_id']} {row['ts']} {active}")
    return 0


def _parse_iso(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resolve_dataset_path(dataset: str | None) -> Path | None:
    if dataset:
        return Path(dataset)
    env_dataset = _read_replay_dataset_env()
    if env_dataset:
        return Path(env_dataset)
    default_path = Path("data") / "replay"
    if default_path.exists():
        return default_path
    return None


def _read_replay_dataset_env() -> str | None:
    import os

    raw = os.getenv("BTCTBOT_REPLAY_DATASET")
    return raw.strip() if raw and raw.strip() else None


def run_stage7_backtest(
    settings: Settings,
    *,
    data_path: str | None,
    out_db: str,
    start: str,
    end: str,
    step_seconds: int,
    seed: int,
    cycles: int | None,
    pair_info_json: str | None,
    include_adaptation: bool,
) -> int:
    resolved_dataset = _resolve_dataset_path(data_path)
    if resolved_dataset is None:
        print(
            "stage7-backtest: dataset not found. ACTION: Run "
            r"`python -m btcbot.cli replay-init --dataset .\data\replay`"
        )
        print(r"stage7-backtest: or set env BTCTBOT_REPLAY_DATASET, or pass --dataset explicitly.")
        return 2

    contract = validate_replay_dataset(resolved_dataset)
    if not contract.ok:
        print(f"stage7-backtest: dataset validation failed: {resolved_dataset}")
        for issue in contract.issues:
            if issue.level == "error":
                print(f"stage7-backtest: FAIL - {issue.message}")
        print(r"stage7-backtest: ACTION - python -m btcbot.cli replay-init --dataset .\data\replay")
        return 2

    replay = MarketDataReplay.from_folder(
        data_path=resolved_dataset,
        start_ts=_parse_iso(start),
        end_ts=_parse_iso(end),
        step_seconds=step_seconds,
        seed=seed,
    )
    try:
        pair_info_snapshot = _load_pair_info_snapshot(pair_info_json)
    except ValueError as exc:
        print(str(exc))
        return 2

    runner = Stage7BacktestRunner()
    summary = runner.run(
        settings=settings,
        replay=replay,
        cycles=cycles,
        out_db_path=Path(out_db),
        seed=seed,
        freeze_params=not include_adaptation,
        disable_adaptation=not include_adaptation,
        pair_info_snapshot=pair_info_snapshot,
    )
    print(json.dumps(summary.__dict__, sort_keys=True))
    return 0


def run_stage7_parity(
    *,
    db_a: str,
    db_b: str,
    start: str,
    end: str,
    dataset: str | None = None,
    quantize_try: str | None = None,
    include_adaptation: bool = False,
) -> int:
    if dataset:
        print(
            "stage7-parity compares two DBs. To generate DBs from a dataset "
            "use stage7-backtest, or use stage7-parity-run (if implemented)."
        )
        return 2

    start_dt = _parse_iso(start)
    end_dt = _parse_iso(end)
    try:
        quantize = _parse_optional_quantize(quantize_try)
    except ValueError as exc:
        print(str(exc))
        return 2
    missing_a = find_missing_stage7_parity_tables(db_a)
    missing_b = find_missing_stage7_parity_tables(db_b)
    if missing_a or missing_b:
        hint = (
            "One or both DBs are missing Stage7 parity tables. "
            "Generate DBs with stage7-backtest to compare full parity."
        )
        print(hint)
        if missing_a:
            print(f"db_a missing tables: {', '.join(missing_a)}")
        if missing_b:
            print(f"db_b missing tables: {', '.join(missing_b)}")

    f1 = compute_run_fingerprint(
        db_a,
        start_dt,
        end_dt,
        quantize_try=quantize,
        include_adaptation=include_adaptation,
    )
    f2 = compute_run_fingerprint(
        db_b,
        start_dt,
        end_dt,
        quantize_try=quantize,
        include_adaptation=include_adaptation,
    )
    ok = compare_fingerprints(f1, f2)
    print(json.dumps({"fingerprint_a": f1, "fingerprint_b": f2, "match": ok}, sort_keys=True))
    return 0 if ok else 1


def run_stage7_backtest_export(
    *,
    settings: Settings,
    db_path: str | None,
    last: int,
    export_format: str,
    out_path: str | None,
    explicit_last: bool = True,
) -> int:
    if not explicit_last:
        print("exporting last 50 rows", file=sys.stderr)

    resolved_db_path = _resolve_stage7_db_path(
        "stage7-backtest-export", db_path=db_path, settings_db_path=settings.state_db_path
    )
    if resolved_db_path is None:
        return 2

    return run_stage7_export(
        settings=settings,
        db_path=resolved_db_path,
        last=last,
        export_format=export_format,
        out_path=out_path,
    )


def _argument_was_provided(flag: str) -> bool:
    argv = getattr(sys, "argv", [])
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in argv[1:])


def _load_pair_info_snapshot(path: str | None) -> list[PairInfo | dict[str, object]] | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"pair info file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("--pair-info-json must contain valid JSON") from exc

    if not isinstance(payload, list):
        raise ValueError("--pair-info-json must be a JSON array")
    snapshot: list[PairInfo | dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            snapshot.append(PairInfo.model_validate(item))
        except Exception:
            snapshot.append(dict(item))
    return snapshot


def _parse_optional_quantize(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        quant = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("--quantize-try must be a decimal step, e.g. 0.01") from exc
    if quant <= 0:
        raise ValueError("--quantize-try must be > 0")
    return quant


def run_stage7_db_count(*, settings: Settings, db_path: str | None) -> int:
    resolved_db_path = _resolve_stage7_db_path(
        "stage7-db-count", db_path=db_path, settings_db_path=settings.state_db_path
    )
    if resolved_db_path is None:
        return 2

    tracked_tables = [
        "stage7_cycle_trace",
        "stage7_ledger_metrics",
        "stage7_run_metrics",
        "stage7_param_changes",
        "stage7_params_checkpoints",
        "stage7_params_active",
    ]

    with single_instance_lock(db_path=resolved_db_path, account_key="stage7-db-count"):
        with sqlite_connection_context(resolved_db_path) as connection:
            cursor = connection.cursor()
            for table_name in tracked_tables:
                exists = cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                    (table_name,),
                ).fetchone()
                if exists is None:
                    print(f"{table_name}: n/a")
                    continue
                count = cursor.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                print(f"{table_name}: {count}")
    return 0


def run_replay_init(*, dataset_path: str, seed: int, write_synthetic: bool) -> int:
    init_replay_dataset(dataset_path=Path(dataset_path), seed=seed, write_synthetic=write_synthetic)
    report = validate_replay_dataset(Path(dataset_path))
    if report.ok:
        print(f"replay-init: OK - initialized dataset at {dataset_path}")
        return 0
    print(f"replay-init: FAIL - dataset at {dataset_path} failed validation")
    for issue in report.issues:
        print(f"replay-init: {issue.level.upper()} - {issue.message}")
    return 1


def run_replay_capture(
    *, dataset_path: str, symbols_csv: str, seconds: int, interval_seconds: int
) -> int:
    symbols = [token.strip().upper() for token in symbols_csv.split(",") if token.strip()]
    if not symbols:
        print("replay-capture: --symbols must include at least one symbol")
        return 2

    capture_replay_dataset(
        ReplayCaptureConfig(
            dataset=Path(dataset_path),
            symbols=symbols,
            seconds=seconds,
            interval_seconds=interval_seconds,
        )
    )
    print(f"replay-capture: OK - captured {len(symbols)} symbol(s) into {dataset_path}")
    return 0


def _doctor_report_json(report: DoctorReport) -> str:
    status = doctor_status(report).upper()
    payload = {
        "status": status,
        "ok": report.ok,
        "checks": [check.__dict__ for check in report.checks],
        "warnings": report.warnings,
        "errors": report.errors,
        "actions": report.actions,
    }
    return json.dumps(redact_data(payload), sort_keys=True)


def run_doctor(
    settings: Settings,
    *,
    db_path: str | None,
    dataset_path: str | None,
    json_output: bool = False,
) -> int:
    if db_path:
        with single_instance_lock(db_path=db_path, account_key="doctor"):
            report = run_health_checks(settings, db_path=db_path, dataset_path=dataset_path)
    else:
        report = run_health_checks(settings, db_path=db_path, dataset_path=dataset_path)
    status = doctor_status(report)

    if json_output:
        print(_doctor_report_json(report))
    else:
        for check in report.checks:
            print(
                f"doctor: {check.status.upper()} [{check.category}] {check.name} - {check.message}"
            )

        check_messages = {check.message for check in report.checks}
        for message in report.warnings:
            if message in check_messages:
                continue
            print(f"doctor: WARN - {message}")
        for message in report.errors:
            if message in check_messages:
                continue
            print(f"doctor: FAIL - {message}")

        if report.actions:
            for action in report.actions:
                print(f"doctor: ACTION - {action}")
        print(f"doctor_status={status.upper()}")

    if status == "fail":
        return 2
    if status == "warn":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
