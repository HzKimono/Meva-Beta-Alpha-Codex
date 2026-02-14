from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from btcbot.accounting.accounting_service import AccountingService
from btcbot.adapters.btcturk_http import (
    BtcturkHttpClient,
    ConfigurationError,
)
from btcbot.config import Settings
from btcbot.domain.models import normalize_symbol
from btcbot.logging_utils import setup_logging
from btcbot.risk.exchange_rules import MarketDataExchangeRulesProvider
from btcbot.risk.policy import RiskPolicy
from btcbot.services.exchange_factory import build_exchange_stage3
from btcbot.services.execution_service import ExecutionService
from btcbot.services.market_data_service import MarketDataService
from btcbot.services.portfolio_service import PortfolioService
from btcbot.services.risk_service import RiskService
from btcbot.services.stage4_cycle_runner import (
    Stage4ConfigurationError,
    Stage4CycleRunner,
    Stage4ExchangeError,
)
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
    parser = argparse.ArgumentParser(prog="btcbot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one decision cycle")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not place orders")

    stage4_run_parser = subparsers.add_parser("stage4-run", help="Run one Stage 4 cycle")
    stage4_run_parser.add_argument("--dry-run", action="store_true", help="Do not place orders")

    stage7_run_parser = subparsers.add_parser("stage7-run", help="Run one Stage 7 dry-run cycle")
    stage7_run_parser.add_argument("--dry-run", action="store_true", help="Required for stage7")

    subparsers.add_parser("health", help="Check exchange connectivity")

    report_parser = subparsers.add_parser("stage7-report", help="Print recent Stage 7 metrics")
    report_parser.add_argument("--last", type=int, default=10)

    export_parser = subparsers.add_parser("stage7-export", help="Export recent Stage 7 metrics")
    export_parser.add_argument("--last", type=int, default=50)
    export_parser.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export_parser.add_argument("--out", required=True)

    alerts_parser = subparsers.add_parser("stage7-alerts", help="Print recent Stage 7 alert cycles")
    alerts_parser.add_argument("--last", type=int, default=50)

    args = parser.parse_args()
    settings = Settings()
    setup_logging(settings.log_level)

    if args.command == "run":
        return run_cycle(settings, force_dry_run=args.dry_run)

    if args.command == "stage4-run":
        return run_cycle_stage4(settings, force_dry_run=args.dry_run)

    if args.command == "stage7-run":
        return run_cycle_stage7(settings, force_dry_run=args.dry_run)

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


def run_cycle_stage7(settings: Settings, force_dry_run: bool = False) -> int:
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
        return runner.run_one_cycle(effective_settings)
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


if __name__ == "__main__":
    raise SystemExit(main())
