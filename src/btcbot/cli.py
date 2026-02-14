from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from btcbot.accounting.accounting_service import AccountingService
from btcbot.adapters.btcturk_http import (
    BtcturkHttpClient,
    ConfigurationError,
)
from btcbot.config import Settings
from btcbot.domain.models import normalize_symbol
from btcbot.logging_utils import setup_logging
from btcbot.replay import ReplayCaptureConfig, capture_replay_dataset, init_replay_dataset
from btcbot.replay.validate import validate_replay_dataset
from btcbot.risk.exchange_rules import MarketDataExchangeRulesProvider
from btcbot.risk.policy import RiskPolicy
from btcbot.services.doctor import DoctorReport, run_health_checks
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
from btcbot.services.risk_service import RiskService
from btcbot.services.stage4_cycle_runner import (
    Stage4ConfigurationError,
    Stage4CycleRunner,
    Stage4ExchangeError,
)
from btcbot.services.stage7_backtest_runner import Stage7BacktestRunner
from btcbot.services.stage7_cycle_runner import Stage7CycleRunner
from btcbot.services.state_store import StateStore
from btcbot.services.strategy_service import StrategyService
from btcbot.services.sweep_service import SweepService
from btcbot.services.trading_policy import (
    PolicyBlockReason,
    policy_block_message,
    validate_live_side_effects_policy,
)
from btcbot.strategies.profit_v1 import ProfitAwareStrategyV1

logger = logging.getLogger(__name__)

LIVE_TRADING_NOT_ARMED_MESSAGE = policy_block_message(PolicyBlockReason.LIVE_NOT_ARMED)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="btcbot",
        epilog=(
            "PowerShell quickstart: stage7-backtest --dataset ./data/replay "
            "--out ./backtest.db ... | "
            "stage7-parity --out-a ./a.db --out-b ./b.db ... | "
            "stage7-backtest-report --db ./backtest.db --out out.jsonl | "
            "stage7-db-count --db ./backtest.db"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one decision cycle")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not place orders")

    stage4_run_parser = subparsers.add_parser("stage4-run", help="Run one Stage 4 cycle")
    stage4_run_parser.add_argument("--dry-run", action="store_true", help="Do not place orders")

    stage7_run_parser = subparsers.add_parser("stage7-run", help="Run one Stage 7 dry-run cycle")
    stage7_run_parser.add_argument("--dry-run", action="store_true", help="Required for stage7")
    stage7_run_parser.add_argument(
        "--include-adaptation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable adaptation evaluation and parameter persistence during this cycle",
    )

    subparsers.add_parser("health", help="Check exchange connectivity")

    report_parser = subparsers.add_parser("stage7-report", help="Print recent Stage 7 metrics")
    report_parser.add_argument("--last", type=int, default=10)

    export_parser = subparsers.add_parser("stage7-export", help="Export recent Stage 7 metrics")
    export_parser.add_argument("--last", type=int, default=50)
    export_parser.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export_parser.add_argument("--out", required=True)

    alerts_parser = subparsers.add_parser("stage7-alerts", help="Print recent Stage 7 alert cycles")
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

    doctor_parser = subparsers.add_parser("doctor", help="Validate local config, env, and DB")
    doctor_parser.add_argument("--db", default=None, help="Optional sqlite DB path to validate")
    doctor_parser.add_argument(
        "--dataset",
        default=None,
        help="Optional replay dataset folder path to validate for backtests",
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON report"
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
    backtest_export.add_argument("--db", required=True)
    backtest_export.add_argument("--out", required=True)
    backtest_export.add_argument("--last", type=int, default=50)
    backtest_export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")

    backtest_count = subparsers.add_parser(
        "stage7-db-count",
        help="Print row counts for Stage 7 tables in a sqlite DB",
    )
    backtest_count.add_argument("--db", required=True)

    args = parser.parse_args()
    settings = Settings()
    setup_logging(settings.log_level)

    if args.command == "run":
        return run_cycle(settings, force_dry_run=args.dry_run)

    if args.command == "stage4-run":
        return run_cycle_stage4(settings, force_dry_run=args.dry_run)

    if args.command == "stage7-run":
        return run_cycle_stage7(
            settings,
            force_dry_run=args.dry_run,
            include_adaptation=args.include_adaptation,
        )

    if args.command == "health":
        return run_health(settings)

    if args.command == "stage7-report":
        return run_stage7_report(settings, last=args.last)

    if args.command == "stage7-export":
        return run_stage7_export(
            settings, last=args.last, export_format=args.format, out_path=args.out
        )

    if args.command == "stage7-alerts":
        return run_stage7_alerts(settings, last=args.last)

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
            db_path=args.db,
            last=args.last,
            export_format=args.format,
            out_path=args.out,
            explicit_last=_argument_was_provided("--last"),
        )

    if args.command == "stage7-db-count":
        return run_stage7_db_count(db_path=args.db)

    if args.command == "doctor":
        return run_doctor(
            settings=settings,
            db_path=args.db,
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


def run_cycle(settings: Settings, force_dry_run: bool = False) -> int:
    dry_run = force_dry_run or settings.dry_run
    live_block_reason = validate_live_side_effects_policy(
        dry_run=dry_run,
        kill_switch=settings.kill_switch,
        live_trading_enabled=settings.is_live_trading_enabled(),
    )
    if not dry_run and live_block_reason is not None:
        block_message = policy_block_message(live_block_reason)
        logger.error(
            block_message,
            extra={"extra": {"reason": live_block_reason.value}},
        )
        print(block_message)
        return 2

    exchange = build_exchange_stage3(settings, force_dry_run=dry_run)
    state_store = StateStore(db_path=settings.state_db_path)
    cycle_id = uuid4().hex

    try:
        if settings.kill_switch:
            logger.warning(
                "Kill switch enabled; planning/logging continue "
                "but cancellations and execution are blocked"
            )

        portfolio_service = PortfolioService(exchange)
        market_data_service = MarketDataService(exchange)
        sweep_service = SweepService(
            state_store=state_store,
            target_try=settings.target_try,
            offset_bps=settings.offset_bps,
            default_min_notional=settings.min_order_notional_try,
        )
        execution_service = ExecutionService(
            exchange=exchange,
            state_store=state_store,
            market_data_service=market_data_service,
            dry_run=dry_run,
            ttl_seconds=settings.ttl_seconds,
            kill_switch=settings.kill_switch,
            live_trading_enabled=settings.is_live_trading_enabled(),
        )
        accounting_service = AccountingService(exchange=exchange, state_store=state_store)
        strategy_service = StrategyService(
            strategy=ProfitAwareStrategyV1(),
            settings=settings,
            market_data_service=market_data_service,
            accounting_service=accounting_service,
            state_store=state_store,
        )
        risk_service = RiskService(
            risk_policy=RiskPolicy(
                rules_provider=MarketDataExchangeRulesProvider(market_data_service),
                max_orders_per_cycle=settings.max_orders_per_cycle,
                max_open_orders_per_symbol=settings.max_open_orders_per_symbol,
                cooldown_seconds=settings.cooldown_seconds,
                notional_cap_try_per_cycle=Decimal(str(settings.notional_cap_try_per_cycle)),
            ),
            state_store=state_store,
        )

        execution_service.cancel_stale_orders(cycle_id=cycle_id)

        balances = portfolio_service.get_balances()
        bids = market_data_service.get_best_bids(settings.symbols)

        mark_prices = {
            normalize_symbol(symbol): Decimal(str(price))
            for symbol, price in bids.items()
            if price > 0
        }
        fills_inserted = accounting_service.refresh(settings.symbols, mark_prices)
        raw_intents = strategy_service.generate(
            cycle_id=cycle_id, symbols=settings.symbols, balances=balances
        )
        approved_intents = risk_service.filter(cycle_id=cycle_id, intents=raw_intents)

        _ = sweep_service.build_order_intents(
            cycle_id=cycle_id,
            balances=balances,
            symbols=settings.symbols,
            best_bids=bids,
        )

        placed = execution_service.execute_intents(approved_intents, cycle_id=cycle_id)
        state_store.set_last_cycle_id(cycle_id)
        logger.info(
            "Cycle completed",
            extra={
                "extra": {
                    "cycle_id": cycle_id,
                    "raw_intents": len(raw_intents),
                    "approved_intents": len(approved_intents),
                    "orders": placed,
                    "fills_inserted": fills_inserted,
                    "positions": len(accounting_service.get_positions()),
                    "dry_run": dry_run,
                    "kill_switch": settings.kill_switch,
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
        _close_best_effort(exchange, "exchange")


def run_cycle_stage4(settings: Settings, force_dry_run: bool = False) -> int:
    dry_run = force_dry_run or settings.dry_run
    live_block_reason = validate_live_side_effects_policy(
        dry_run=dry_run,
        kill_switch=settings.kill_switch,
        live_trading_enabled=settings.is_live_trading_enabled(),
    )
    cycle_runner = Stage4CycleRunner()
    effective_settings = settings.model_copy(update={"dry_run": dry_run})
    cycle_id = uuid4().hex
    if not dry_run and live_block_reason is not None:
        block_message = policy_block_message(live_block_reason)
        logger.error(block_message, extra={"extra": {"reason": live_block_reason.value}})
        print(block_message)
        StateStore(db_path=settings.state_db_path).record_cycle_audit(
            cycle_id=cycle_id,
            counts={"blocked_by_policy": 1},
            decisions=[f"policy_block:{live_block_reason.value}"],
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
        logger.info("Running Stage 4 cycle")
        result = cycle_runner.run_one_cycle(effective_settings)
        return result
    except Stage4ConfigurationError as exc:
        logger.exception(
            "Stage 4 cycle failed due to configuration error",
            extra={"extra": {"error_type": type(exc).__name__, "safe_message": str(exc)}},
        )
        return 2
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
) -> int:
    dry_run = force_dry_run or settings.dry_run
    if not dry_run:
        print("stage7-run requires --dry-run")
        return 2
    if not settings.stage7_enabled:
        print("stage7-run is disabled; set STAGE7_ENABLED=true to run")
        logger.warning("stage7_disabled_in_settings")
        return 2
    runner = Stage7CycleRunner()
    effective_settings = settings.model_copy(update={"dry_run": True})
    try:
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


def run_health(settings: Settings) -> int:
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
        print(f"BTCTurk public API health: {status}")
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check failed", extra={"extra": {"error_type": type(exc).__name__}})
        print("BTCTurk public API health: FAIL")
        return 1
    finally:
        _close_best_effort(client, "health client")


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


def _csv_safe_value(value: object) -> object:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def run_stage7_report(settings: Settings, last: int) -> int:
    store = StateStore(db_path=settings.state_db_path)
    rows = store.fetch_stage7_run_metrics(limit=last, order_desc=True)
    print("cycle_id ts mode net_pnl_try max_dd turnover intents rejects throttled")
    for row in rows:
        print(
            f"{row['cycle_id']} {row['ts']} {row['mode_final']} "
            f"{row['net_pnl_try']} {row['max_drawdown_pct']} {row['turnover_try']} "
            f"{row['intents_planned_count']} {row['oms_rejected_count']} "
            f"{int(row.get('oms_throttled_count', 0))}"
        )
    return 0


def run_stage7_export(settings: Settings, last: int, export_format: str, out_path: str) -> int:
    store = StateStore(db_path=settings.state_db_path)
    rows = store.fetch_stage7_cycles_for_export(limit=last)
    if export_format == "jsonl":
        with open(out_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        return 0

    fieldnames = sorted({key for row in rows for key in row.keys()})
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            normalized = {key: _csv_safe_value(value) for key, value in row.items()}
            writer.writerow(normalized)
    return 0


def run_stage7_alerts(settings: Settings, last: int) -> int:
    store = StateStore(db_path=settings.state_db_path)
    rows = store.fetch_stage7_run_metrics(limit=last, order_desc=True)
    print("cycle_id ts alerts")
    for row in rows:
        alerts = row.get("alert_flags", {})
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
    *, db_path: str, last: int, export_format: str, out_path: str, explicit_last: bool = True
) -> int:
    if not explicit_last:
        print("exporting last 50 rows", file=sys.stderr)

    store = StateStore(db_path=db_path)
    rows = store.fetch_stage7_cycles_for_export(limit=last)
    if export_format == "jsonl":
        with open(out_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        return 0

    fieldnames = sorted({key for row in rows for key in row.keys()})
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            normalized = {key: _csv_safe_value(value) for key, value in row.items()}
            writer.writerow(normalized)
    return 0


def _argument_was_provided(flag: str) -> bool:
    argv = getattr(sys, "argv", [])
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in argv[1:])


def _load_pair_info_snapshot(path: str | None) -> list[dict[str, object]] | None:
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
    return [dict(item) for item in payload if isinstance(item, dict)]


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


def run_stage7_db_count(*, db_path: str) -> int:
    tracked_tables = [
        "stage7_cycle_trace",
        "stage7_ledger_metrics",
        "stage7_run_metrics",
        "stage7_param_changes",
        "stage7_params_checkpoints",
        "stage7_params_active",
    ]

    with sqlite3.connect(db_path) as connection:
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
    payload = {
        "status": "ok" if report.ok else "fail",
        "checks": [check.__dict__ for check in report.checks],
        "warnings": report.warnings,
        "errors": report.errors,
        "actions": report.actions,
    }
    return json.dumps(payload, sort_keys=True)


def run_doctor(
    settings: Settings,
    *,
    db_path: str | None,
    dataset_path: str | None,
    json_output: bool = False,
) -> int:
    report = run_health_checks(settings, db_path=db_path, dataset_path=dataset_path)

    if json_output:
        print(_doctor_report_json(report))
        return 0 if report.ok else 1

    for check in report.checks:
        print(f"doctor: {check.status.upper()} [{check.category}] {check.name} - {check.message}")

    for message in report.warnings:
        print(f"doctor: WARN - {message}")
    for message in report.errors:
        print(f"doctor: FAIL - {message}")

    if report.actions:
        for action in report.actions:
            print(f"doctor: ACTION - {action}")

    if report.ok:
        print("doctor: OK")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
