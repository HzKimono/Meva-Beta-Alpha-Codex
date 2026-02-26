from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import cast
from uuid import uuid4

from btcbot.adapters.exchange import ExchangeClient
from btcbot.adapters.replay_exchange import ReplayExchangeClient
from btcbot.config import Settings
from btcbot.domain.accounting import Position, TradeFill
from btcbot.domain.anomalies import combine_modes
from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.domain.models import Balance, normalize_symbol
from btcbot.domain.models import OrderSide as DomainOrderSide
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.portfolio_policy_models import PortfolioPlan
from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_mode_codec import dump_risk_mode
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType
from btcbot.logging_context import with_cycle_context
from btcbot.obs.metrics import observe_histogram, set_gauge
from btcbot.obs.process_role import coerce_process_role
from btcbot.planning_kernel import ExecutionPort, Plan, PlanningKernel
from btcbot.services.adaptation_service import AdaptationService
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.exposure_tracker import ExposureTracker
from btcbot.services.ledger_service import LedgerService
from btcbot.services.metrics_collector import MetricsCollector
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.order_builder_service import OrderBuilderService
from btcbot.services.planning_kernel_adapters import Stage7ExecutionPort, Stage7PlanConsumer
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.stage7_planning_kernel_integration import (
    Stage7OrderIntentBuilderAdapter,
    Stage7PassThroughAllocator,
    Stage7PortfolioStrategyAdapter,
    Stage7UniverseSelectorAdapter,
    build_stage7_planning_context,
    normalize_stage4_open_orders,
)
from btcbot.services.stage7_risk_budget_service import Stage7RiskBudgetService, Stage7RiskInputs
from btcbot.services.risk_policy_service import (
    ActionPortfolioSnapshot,
    RiskPolicyService,
)
from btcbot.services.state_store import StateStore
from btcbot.services.universe_selection_service import _BPS, UniverseSelectionService

logger = logging.getLogger(__name__)

_PLANNING_DISABLED_REASONS = {
    "STRATEGY_DISABLED",
    "PLANNER_NOT_WIRED",
    "DATA_UNAVAILABLE",
    "BUDGET_ZERO",
    "ALL_INTENTS_REJECTED_BY_RISK",
    "MIN_NOTIONAL_ALL_REJECTED",
    "SIM_EXECUTION_DISABLED",
    "UNKNOWN",
}


def _deterministic_fill_id(cycle_id: str, client_order_id: str, symbol: str, side: str) -> str:
    digest = sha256(f"{cycle_id}|{client_order_id}|{symbol}|{side}".encode()).hexdigest()[:16]
    return f"s7f:{digest}"


def _resolve_planning_disabled_reason(
    *,
    planning_enabled: bool,
    selected_universe: list[str],
    mark_prices: dict[str, Decimal],
    notional_cap_try_per_cycle: Decimal,
    order_intents: list[OrderIntent],
) -> str | None:
    if planning_enabled:
        return None
    if not selected_universe or not mark_prices:
        return "DATA_UNAVAILABLE"
    if notional_cap_try_per_cycle <= 0:
        return "BUDGET_ZERO"

    skipped = [item for item in order_intents if bool(getattr(item, "skipped", False))]
    if skipped and all(
        str(getattr(item, "skip_reason", "")).startswith("risk_") for item in skipped
    ):
        return "ALL_INTENTS_REJECTED_BY_RISK"
    if skipped and all(
        "min_notional" in str(getattr(item, "skip_reason", "")).lower() for item in skipped
    ):
        return "MIN_NOTIONAL_ALL_REJECTED"
    return "UNKNOWN"


def _coerce_int(value: object, default: int = 0) -> int:
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


def _to_risk_action(intent: OrderIntent) -> LifecycleAction:
    return LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol=normalize_symbol(intent.symbol),
        side=str(intent.side),
        price=intent.price_try,
        qty=intent.qty,
        reason=str(intent.reason),
        client_order_id=intent.client_order_id,
    )


def _filter_order_intents_by_risk(
    *,
    order_intents: list[OrderIntent],
    risk_policy_service: RiskPolicyService,
    portfolio_snapshot: ActionPortfolioSnapshot,
    cycle_risk,
) -> tuple[list[OrderIntent], list[dict[str, object]]]:
    intent_risk_actions = [_to_risk_action(intent) for intent in order_intents if not intent.skipped]
    allowed_intent_actions, intent_decisions = risk_policy_service.filter_actions(
        actions=intent_risk_actions,
        portfolio=portfolio_snapshot,
        cycle_risk=cycle_risk,
    )
    allowed_intent_ids = {
        action.client_order_id for action in allowed_intent_actions if action.client_order_id
    }
    submitted_intents: list[OrderIntent] = []
    skipped_actions: list[dict[str, object]] = []
    for intent in order_intents:
        if intent.skipped:
            submitted_intents.append(intent)
            continue
        if intent.client_order_id in allowed_intent_ids:
            submitted_intents.append(intent)
            continue
        blocked_reason = next(
            (
                decision.reason
                for decision in intent_decisions
                if decision.action.client_order_id == intent.client_order_id and not decision.accepted
            ),
            "risk_blocked",
        )
        submitted_intents.append(
            OrderIntent(
                cycle_id=intent.cycle_id,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                price_try=intent.price_try,
                qty=intent.qty,
                notional_try=intent.notional_try,
                client_order_id=intent.client_order_id,
                reason=intent.reason,
                constraints_applied=intent.constraints_applied,
                skipped=True,
                skip_reason=blocked_reason,
            )
        )
        skipped_actions.append(
            {
                "symbol": normalize_symbol(intent.symbol),
                "side": intent.side,
                "qty": str(intent.qty),
                "status": "skipped",
                "reason": blocked_reason,
                "client_order_id": intent.client_order_id,
            }
        )
    return submitted_intents, skipped_actions


class Stage7CycleRunner:
    command: str = "stage7-run"

    def run_one_cycle(
        self,
        settings: Settings,
        *,
        exchange: object | None = None,
        state_store: StateStore | None = None,
        now_utc: datetime | None = None,
        cycle_id: str | None = None,
        run_id: str | None = None,
        stage4_result: int | None = None,
        enable_adaptation: bool = True,
        use_active_params: bool = True,
    ) -> int:
        if not settings.dry_run:
            raise RuntimeError("stage7-run only supports --dry-run")

        resolved_store = state_store or StateStore(db_path=settings.state_db_path)
        resolved_exchange = exchange or build_exchange_stage4(settings, dry_run=True)
        should_close_exchange = exchange is None

        if stage4_result is None:
            stage4 = Stage4CycleRunner(command=self.command)
            stage4_result = stage4.run_one_cycle(settings)

        resolved_now = now_utc or datetime.now(UTC)
        resolved_cycle_id = cycle_id or uuid4().hex
        resolved_run_id = run_id or uuid4().hex
        process_role = coerce_process_role(getattr(settings, "process_role", None)).value
        try:
            self.run_one_cycle_with_dependencies(
                settings=settings,
                exchange=resolved_exchange,
                state_store=resolved_store,
                now_utc=resolved_now,
                cycle_id=resolved_cycle_id,
                run_id=resolved_run_id,
                stage4_result=stage4_result,
                enable_adaptation=enable_adaptation,
                use_active_params=use_active_params,
            )
            api_health = "healthy"
            snapshot_fn = getattr(resolved_exchange, "health_snapshot", None)
            if callable(snapshot_fn):
                snapshot = snapshot_fn() or {}
                if bool(snapshot.get("degraded") or snapshot.get("breaker_open")):
                    api_health = "degraded"
            if api_health == "healthy":
                resolved_store.reset_consecutive_critical_errors(process_role)
        except Exception as exc:  # noqa: BLE001
            next_count = resolved_store.increment_consecutive_critical_errors(process_role)
            if next_count >= int(getattr(settings, "kill_chain_max_consecutive_errors", 3)):
                cooldown_seconds = int(getattr(settings, "kill_chain_cooldown_seconds", 0))
                until_ts = None
                if cooldown_seconds > 0:
                    until_ts = (datetime.now(UTC) + timedelta(seconds=cooldown_seconds)).isoformat()
                resolved_store.set_kill_switch(
                    process_role,
                    True,
                    f"kill_chain:{type(exc).__name__}",
                    until_ts,
                )
            raise

        if should_close_exchange:
            close = getattr(resolved_exchange, "close", None)
            if callable(close):
                close()

        return stage4_result

    def run_one_cycle_with_dependencies(
        self,
        *,
        settings: Settings,
        exchange: object,
        state_store: StateStore,
        now_utc: datetime,
        cycle_id: str,
        run_id: str,
        stage4_result: int = 0,
        enable_adaptation: bool = True,
        use_active_params: bool = True,
    ) -> int:
        now = now_utc.astimezone(UTC)
        collector = MetricsCollector()
        process_role = coerce_process_role(getattr(settings, "process_role", None)).value
        collector.set("run_id", run_id)
        collector.set("ts", now.isoformat())

        adaptation_service = AdaptationService()
        stage4 = Stage4CycleRunner(command=self.command)
        runtime = settings.model_copy(deep=True)
        active_params = None
        if use_active_params:
            active_params = state_store.get_active_stage7_params(settings=settings, now_utc=now)
            runtime.stage7_universe_size = active_params.universe_size
            runtime.stage7_score_weights = {
                k: float(v) for k, v in active_params.score_weights.items()
            }
            runtime.stage7_max_spread_bps = Decimal(str(active_params.max_spread_bps))
            runtime.notional_cap_try_per_cycle = active_params.turnover_cap_try
            runtime.max_orders_per_cycle = active_params.max_orders_per_cycle
            runtime.try_cash_target = active_params.cash_target_try
            runtime.stage7_order_offset_bps = Decimal(str(active_params.order_offset_bps))
            runtime.stage7_min_quote_volume_try = active_params.min_quote_volume_try

        ledger_service = LedgerService(state_store=state_store, logger=logger)
        universe_service = UniverseSelectionService()
        policy_service = PortfolioPolicyService()
        order_builder = OrderBuilderService()
        exposure_tracker = ExposureTracker()
        risk_budget_service = Stage7RiskBudgetService()
        risk_policy_service = RiskPolicyService()

        open_orders = state_store.list_stage4_open_orders()
        lifecycle_actions: list[LifecycleAction] = []
        for order in open_orders:
            if order.status.lower() == "simulated_submitted":
                lifecycle_actions.append(
                    LifecycleAction(
                        action_type=LifecycleActionType.SUBMIT,
                        symbol=normalize_symbol(order.symbol),
                        side=order.side,
                        price=order.price,
                        qty=order.qty,
                        reason="stage7_dry_run_simulation",
                        client_order_id=order.client_order_id,
                        exchange_order_id=order.exchange_order_id,
                    )
                )

        base_client = getattr(exchange, "client", exchange)
        exchange_client = cast(ExchangeClient, base_client)
        is_backtest_simulation = isinstance(base_client, ReplayExchangeClient)
        rules_service = ExchangeRulesService(
            exchange_client,
            cache_ttl_sec=settings.rules_cache_ttl_sec,
            settings=settings,
        )

        collector.start_timer("cycle_total")
        with with_cycle_context(cycle_id=cycle_id, run_id=run_id):
            logger.info(
                "stage7_cycle_start", extra={"extra": {"cycle_id": cycle_id, "run_id": run_id}}
            )
            logger.info(
                "stage7_effective_settings",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "priority_order": ["defaults", "env", "stage7_params_active"],
                        "try_cash_target": str(runtime.try_cash_target),
                        "try_cash_max": str(runtime.try_cash_max),
                        "max_orders_per_cycle": runtime.max_orders_per_cycle,
                        "notional_cap_try_per_cycle": str(runtime.notional_cap_try_per_cycle),
                        "stage7_order_offset_bps": str(runtime.stage7_order_offset_bps),
                        "stage7_max_drawdown_ratio": str(runtime.stage7_max_drawdown_pct),
                        "pnl_divergence_try_warn": str(runtime.pnl_divergence_try_warn),
                        "pnl_divergence_try_error": str(runtime.pnl_divergence_try_error),
                        "active_param_version": active_params.version if active_params else 0,
                    }
                },
            )
            collector.start_timer("selection")
            bootstrap_symbols = sorted({normalize_symbol(symbol) for symbol in settings.symbols})
            bootstrap_marks, _ = stage4.resolve_mark_prices(exchange, bootstrap_symbols)
            get_balances = getattr(base_client, "get_balances", None)
            balances = get_balances() if callable(get_balances) else []
            if not balances:
                balances = [
                    Balance(
                        asset=str(settings.stage7_universe_quote_ccy).upper(),
                        free=settings.dry_run_try_balance,
                    )
                ]

            exposure_snapshot = exposure_tracker.compute_snapshot(
                balances=balances,
                mark_prices_try=bootstrap_marks,
                settings=runtime,
                now_utc=now,
                plan=None,
            )

            latest_metrics = state_store.get_latest_stage7_ledger_metrics() or {
                "max_drawdown_ratio": Decimal("0"),
                "net_pnl_try": Decimal("0"),
                "equity_try": Decimal(str(settings.dry_run_try_balance)),
            }
            previous_risk_decision = state_store.get_latest_stage7_risk_decision()
            spread_bps = Decimal("0")
            quote_volume_try = Decimal("0")
            observed_ts: list[datetime] = []
            ticker_stats_getter = getattr(base_client, "get_ticker_stats", None)
            if callable(ticker_stats_getter):
                for row in ticker_stats_getter() or []:
                    if not isinstance(row, dict):
                        continue
                    symbol = normalize_symbol(str(row.get("pairSymbol") or row.get("symbol") or ""))
                    if bootstrap_symbols and symbol not in bootstrap_symbols:
                        continue
                    vol_raw = row.get("volume") or row.get("quoteVolume")
                    if vol_raw is not None:
                        quote_volume_try += Decimal(str(vol_raw))
                    ts_raw = row.get("ts") or row.get("timestamp")
                    if isinstance(ts_raw, (int, float)):
                        observed_ts.append(datetime.fromtimestamp(float(ts_raw), tz=UTC))

            orderbook_getter = getattr(base_client, "get_orderbook", None)
            if callable(orderbook_getter):
                spreads: list[Decimal] = []
                for symbol in bootstrap_symbols:
                    try:
                        bid, ask = orderbook_getter(symbol)
                    except Exception:  # noqa: BLE001
                        continue
                    bid_dec = Decimal(str(bid))
                    ask_dec = Decimal(str(ask))
                    if bid_dec > 0 and ask_dec >= bid_dec:
                        mid = (bid_dec + ask_dec) / Decimal("2")
                        spreads.append(((ask_dec - bid_dec) / mid) * _BPS)
                if spreads:
                    spread_bps = max(spreads)

            data_age_sec = settings.stage7_max_data_age_sec + 1
            if observed_ts:
                newest = max(observed_ts)
                data_age_sec = max(0, int((now - newest).total_seconds()))
            elif quote_volume_try > 0:
                data_age_sec = 0
            elif is_backtest_simulation:
                # Replay datasets may omit ticker timestamps/quote volume. In backtests this
                # must not force OBSERVE_ONLY because execution is fully simulated locally.
                data_age_sec = 0

            risk_inputs = Stage7RiskInputs(
                max_drawdown_pct=latest_metrics["max_drawdown_ratio"],
                daily_pnl_try=latest_metrics["net_pnl_try"],
                consecutive_loss_streak=0,
                market_data_age_sec=data_age_sec,
                observed_spread_bps=spread_bps,
                quote_volume_try=quote_volume_try,
                exposure_snapshot=exposure_snapshot,
                fee_burn_today_try=Decimal(str(latest_metrics.get("fees_try", "0"))),
                reject_rate_window=Decimal("0"),
                exchange_degraded=False,
                stale_age_sec=data_age_sec,
            )
            stage7_risk_decision = risk_budget_service.decide(
                settings=runtime,
                now_utc=now,
                inputs=risk_inputs,
                previous_decision=previous_risk_decision,
            )

            universe_result = universe_service.select_universe(
                exchange=base_client,
                settings=runtime,
                now_utc=now,
            )
            collector.stop_timer("selection")
            universe_syms = {
                normalize_symbol(symbol) for symbol in universe_result.selected_symbols
            }
            lifecycle_syms = {normalize_symbol(action.symbol) for action in lifecycle_actions}
            symbols_needed = sorted(universe_syms | lifecycle_syms)
            mark_prices, _ = stage4.resolve_mark_prices(exchange, symbols_needed)
            ticker_stats_by_symbol: dict[str, dict[str, object]] = {}
            if callable(ticker_stats_getter):
                for row in ticker_stats_getter() or []:
                    if not isinstance(row, dict):
                        continue
                    symbol_key = normalize_symbol(
                        str(row.get("pairSymbol") or row.get("symbol") or row.get("pair") or "")
                    )
                    if symbol_key:
                        ticker_stats_by_symbol[symbol_key] = row
            get_candles = getattr(base_client, "get_candles", None)
            for symbol in symbols_needed:
                if symbol in mark_prices:
                    continue
                if symbol not in universe_syms:
                    continue
                ticker_row = ticker_stats_by_symbol.get(symbol)
                if ticker_row is not None:
                    last_raw = ticker_row.get("last") or ticker_row.get("lastPrice")
                    if last_raw is not None:
                        last_price = Decimal(str(last_raw))
                        if last_price > 0:
                            mark_prices[symbol] = last_price
                            continue
                if callable(get_candles):
                    try:
                        candle_rows = get_candles(symbol, 1)
                    except Exception:  # noqa: BLE001
                        candle_rows = []
                    if candle_rows:
                        latest = candle_rows[-1]
                        if isinstance(latest, dict):
                            close_raw = latest.get("close") or latest.get("c")
                        else:
                            close_raw = getattr(latest, "close", None)
                        if close_raw is not None:
                            close_price = Decimal(str(close_raw))
                            if close_price > 0:
                                mark_prices[symbol] = close_price
            rules_symbols_fallback: set[str] = set()
            rules_symbols_invalid_metadata: set[str] = set()
            rules_symbols_missing: set[str] = set()
            rules_symbols_error: set[str] = set()
            rules_unavailable: dict[str, str] = {}
            rules_unavailable_details: dict[str, str] = {}
            for symbol in symbols_needed:
                decision = rules_service.resolve_boundary(symbol)
                if decision.outcome == "DEGRADE":
                    rules_symbols_fallback.add(symbol)
                    continue
                if decision.outcome == "OK":
                    continue

                status = decision.resolution.status
                detail = decision.reason or status
                rules_unavailable[symbol] = status
                rules_unavailable_details[symbol] = detail
                if status == "invalid_metadata":
                    rules_symbols_invalid_metadata.add(symbol)
                elif status == "missing":
                    rules_symbols_missing.add(symbol)
                else:
                    rules_symbols_error.add(symbol)

            rules_invalid_metadata_count = len(rules_symbols_invalid_metadata)
            rules_missing_count = len(rules_symbols_missing)
            rules_error_count = len(rules_symbols_error)
            rules_stats: dict[str, object] = {
                "rules_fallback_used_count": len(rules_symbols_fallback),
                "rules_invalid_metadata_count": rules_invalid_metadata_count,
                "rules_missing_count": rules_missing_count,
                "rules_error_count": rules_error_count,
                "rules_symbols_fallback": sorted(rules_symbols_fallback),
                "rules_symbols_invalid_metadata": sorted(rules_symbols_invalid_metadata),
                "rules_symbols_missing": sorted(rules_symbols_missing),
                "rules_symbols_error": sorted(rules_symbols_error),
                "rules_unavailable_details": dict(sorted(rules_unavailable_details.items())),
            }

            base_risk_mode = (
                Mode.NORMAL if is_backtest_simulation else state_store.get_latest_risk_mode()
            )
            final_risk_mode = combine_modes(base_risk_mode, None)
            stage7_risk_mode = stage7_risk_decision.mode
            final_risk_mode = combine_modes(final_risk_mode, stage7_risk_mode)
            invalid_policy = settings.stage7_rules_invalid_metadata_policy
            if invalid_policy == "observe_only_cycle" and (
                rules_invalid_metadata_count > 0 or rules_missing_count > 0 or rules_error_count > 0
            ):
                final_risk_mode = Mode.OBSERVE_ONLY

            mode_payload: dict[str, object] = {
                "base_mode": dump_risk_mode(base_risk_mode),
                "override_mode": None,
                "final_mode": dump_risk_mode(final_risk_mode),
                "risk_mode": dump_risk_mode(stage7_risk_mode),
                "risk_reasons": stage7_risk_decision.reasons,
                "risk_cooldown_until": (
                    (getattr(stage7_risk_decision, "cooldown_until_utc", None) or getattr(stage7_risk_decision, "cooldown_until", None)).isoformat()
                    if (getattr(stage7_risk_decision, "cooldown_until_utc", None) or getattr(stage7_risk_decision, "cooldown_until", None))
                    else None
                ),
                "risk_inputs_hash": stage7_risk_decision.inputs_hash,
            }

            collector.start_timer("planning")
            collector.start_timer("intents")
            try:
                portfolio_plan, order_intents, planning_engine = self._build_stage7_order_intents(
                    cycle_id=cycle_id,
                    now=now,
                    runtime=runtime,
                    universe_service=universe_service,
                    base_client=base_client,
                    mark_prices=mark_prices,
                    balances=balances,
                    open_orders=open_orders,
                    final_mode=final_risk_mode,
                    rules_service=rules_service,
                    rules_unavailable=rules_unavailable,
                    selected_universe=universe_result.selected_symbols,
                    policy_service=policy_service,
                    order_builder=order_builder,
                )
            finally:
                collector.stop_timer("intents")
                collector.stop_timer("planning")

            actions: list[dict[str, object]] = []
            slippage_try = Decimal("0")
            oms_orders = []
            oms_events = []
            fills_written_count = 0
            fills_applied_count = 0
            ledger_events_inserted = 0
            positions_updated_count = 0

            if final_risk_mode != Mode.OBSERVE_ONLY:
                filtered_actions: list[LifecycleAction] = []
                skipped_actions: list[dict[str, object]] = []
                for action in lifecycle_actions:
                    normalized_symbol = normalize_symbol(action.symbol)
                    if normalized_symbol in rules_unavailable:
                        unavailable_status = rules_unavailable[normalized_symbol]
                        unavailable_detail = rules_unavailable_details.get(
                            normalized_symbol,
                            unavailable_status,
                        )
                        skipped_actions.append(
                            {
                                "symbol": normalized_symbol,
                                "side": action.side,
                                "qty": str(action.qty),
                                "status": "skipped",
                                "reason": (
                                    f"rules_unavailable:{unavailable_status}:{unavailable_detail}"
                                ),
                            }
                        )
                        continue
                    if normalized_symbol not in mark_prices:
                        skipped_actions.append(
                            {
                                "symbol": normalized_symbol,
                                "side": action.side,
                                "qty": str(action.qty),
                                "status": "skipped",
                                "reason": "missing_mark_price",
                            }
                        )
                        continue
                    filtered_actions.append(action)

                positions_by_symbol: dict[str, Decimal] = {}
                for position in state_store.list_stage4_positions():
                    positions_by_symbol[normalize_symbol(position.symbol)] = position.qty
                portfolio_snapshot = ActionPortfolioSnapshot(positions_by_symbol=positions_by_symbol)
                filtered_actions, policy_decisions = risk_policy_service.filter_actions(
                    actions=filtered_actions,
                    portfolio=portfolio_snapshot,
                    cycle_risk=stage7_risk_decision,
                )
                for decision in policy_decisions:
                    if decision.accepted:
                        continue
                    skipped_actions.append(
                        {
                            "symbol": normalize_symbol(decision.action.symbol),
                            "side": decision.action.side,
                            "qty": str(decision.action.qty),
                            "status": "skipped",
                            "reason": decision.reason,
                        }
                    )

                order_intents, planning_skipped_actions = _filter_order_intents_by_risk(
                    order_intents=order_intents,
                    risk_policy_service=risk_policy_service,
                    portfolio_snapshot=portfolio_snapshot,
                    cycle_risk=stage7_risk_decision,
                )
                skipped_actions.extend(planning_skipped_actions)

                collector.start_timer("oms")
                oms_service = OMSService()
                market_simulator = Stage7MarketSimulator(mark_prices)
                execution_port = Stage7ExecutionPort(
                    cycle_id=cycle_id,
                    now_utc=now,
                    oms_service=oms_service,
                    market_sim=market_simulator,
                    state_store=state_store,
                    settings=runtime,
                )
                kernel_plan = Plan(
                    cycle_id=cycle_id,
                    generated_at=now,
                    universe=tuple(
                        sorted(
                            {normalize_symbol(item) for item in universe_result.selected_symbols}
                        )
                    ),
                    order_intents=tuple(order_intents),
                    planning_gates={},
                    intents_raw=tuple(),
                    intents_allocated=tuple(),
                    diagnostics={"engine": planning_engine},
                )
                self.consume_shared_plan(kernel_plan, execution_port)
                execution_report = execution_port.reconcile()
                oms_orders_raw = execution_report.get("orders", [])
                oms_events_raw = execution_report.get("events", [])
                oms_orders = list(oms_orders_raw) if isinstance(oms_orders_raw, list) else []
                oms_events = list(oms_events_raw) if isinstance(oms_events_raw, list) else []
                collector.stop_timer("oms")
                actions = (
                    [
                        {
                            "symbol": normalize_symbol(action.symbol),
                            "side": action.side,
                            "qty": str(action.qty),
                            "status": "submitted",
                            "reason": "dry_run_fill_simulated",
                        }
                        for action in filtered_actions
                    ]
                    + [
                        {
                            "symbol": normalize_symbol(order.symbol),
                            "side": order.side,
                            "qty": str(order.qty),
                            "status": order.status.value,
                            "reason": "dry_run_oms",
                            "client_order_id": order.client_order_id,
                        }
                        for order in oms_orders
                    ]
                    + skipped_actions
                )
            else:
                actions = [{"status": "skipped", "reason": "observe_only"}]

            materialized_fills: list[TradeFill] = []
            ledger_events: list[LedgerEvent] = []
            for order in sorted(oms_orders, key=lambda item: item.client_order_id):
                if order.status.value != "FILLED":
                    continue
                if order.avg_fill_price_try is None or order.filled_qty <= 0:
                    continue
                symbol = normalize_symbol(order.symbol)
                side = order.side.upper()
                fill_id = _deterministic_fill_id(cycle_id, order.client_order_id, symbol, side)
                fee_try = (order.avg_fill_price_try * order.filled_qty) * (
                    runtime.stage7_fees_bps / Decimal("10000")
                )
                trade_fill = TradeFill(
                    fill_id=fill_id,
                    order_id=order.order_id,
                    symbol=symbol,
                    side=(DomainOrderSide.BUY if side == "BUY" else DomainOrderSide.SELL),
                    price=order.avg_fill_price_try,
                    qty=order.filled_qty,
                    fee=fee_try,
                    fee_currency="TRY",
                    ts=now,
                )
                materialized_fills.append(trade_fill)
                slippage_component = abs(
                    (order.avg_fill_price_try - order.price_try) * order.filled_qty
                )
                slippage_try += slippage_component
                ledger_events.append(
                    LedgerEvent(
                        event_id=f"fill:{fill_id}",
                        ts=now,
                        symbol=symbol,
                        type=LedgerEventType.FILL,
                        side=side,
                        qty=order.filled_qty,
                        price=order.avg_fill_price_try,
                        fee=None,
                        fee_currency=None,
                        exchange_trade_id=fill_id,
                        exchange_order_id=order.order_id,
                        client_order_id=order.client_order_id,
                        meta={"source": "stage7_oms", "cycle_id": cycle_id},
                    )
                )
                ledger_events.append(
                    LedgerEvent(
                        event_id=f"fee:{fill_id}",
                        ts=now,
                        symbol=symbol,
                        type=LedgerEventType.FEE,
                        side=None,
                        qty=Decimal("0"),
                        price=None,
                        fee=fee_try,
                        fee_currency="TRY",
                        exchange_trade_id=f"fee:{fill_id}",
                        exchange_order_id=order.order_id,
                        client_order_id=order.client_order_id,
                        meta={
                            "source": "stage7_oms",
                            "linked_fill_id": fill_id,
                            "cycle_id": cycle_id,
                        },
                    )
                )

            for fill in materialized_fills:
                if state_store.save_fill(fill):
                    fills_written_count += 1
            logger.info(
                "stage7_fills_materialized",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "fills_written_count": fills_written_count,
                        "fills_total": len(materialized_fills),
                    }
                },
            )

            for fill in materialized_fills:
                if fill.fill_id is not None and state_store.mark_fill_applied(fill.fill_id):
                    fills_applied_count += 1

            append_result = state_store.append_ledger_events(ledger_events)
            ledger_events_inserted = append_result.inserted
            logger.info(
                "stage7_ledger_ingested",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "ledger_events_inserted": ledger_events_inserted,
                        "fills_applied_count": fills_applied_count,
                    }
                },
            )

            ledger_state, _, _, _ = ledger_service.load_state_incremental(scope_id="stage7")
            for symbol in sorted(ledger_state.symbols):
                symbol_state = ledger_state.symbols[symbol]
                qty = sum((lot.qty for lot in symbol_state.lots), Decimal("0"))
                notional = sum((lot.qty * lot.unit_cost for lot in symbol_state.lots), Decimal("0"))
                avg_cost = (notional / qty) if qty > 0 else Decimal("0")
                mark = mark_prices.get(symbol, avg_cost)
                unrealized = sum(
                    ((mark - lot.unit_cost) * lot.qty for lot in symbol_state.lots),
                    Decimal("0"),
                )
                fees_paid = ledger_state.fees_by_currency.get("TRY", Decimal("0"))
                state_store.save_position(
                    Position(
                        symbol=symbol,
                        qty=qty,
                        avg_cost=avg_cost,
                        realized_pnl=symbol_state.realized_pnl,
                        unrealized_pnl=unrealized,
                        fees_paid=fees_paid,
                        updated_at=now,
                    )
                )
                positions_updated_count += 1
            logger.info(
                "stage7_positions_updated",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "positions_updated_count": positions_updated_count,
                    }
                },
            )

            collector.start_timer("ledger")
            snapshot = ledger_service.snapshot(
                mark_prices=mark_prices,
                cash_try=exposure_snapshot.free_cash_try,
                slippage_try=slippage_try,
                ts=now,
                ledger_state=ledger_state,
            )

            collector.stop_timer("ledger")

            planned_count = sum(1 for intent in order_intents if not intent.skipped)
            skipped_count = sum(1 for intent in order_intents if intent.skipped)
            oms_submitted = sum(
                1
                for o in oms_orders
                if o.status.value in {"SUBMITTED", "ACKED", "PARTIALLY_FILLED", "FILLED"}
            )
            oms_filled = sum(1 for o in oms_orders if o.status.value == "FILLED")
            oms_rejected = sum(1 for o in oms_orders if o.status.value == "REJECTED")
            oms_canceled = sum(1 for o in oms_orders if o.status.value == "CANCELED")
            missing_mark_price_count = sum(
                1 for action in actions if action.get("reason") == "missing_mark_price"
            )
            throttled_events = sum(1 for event in oms_events if event.event_type == "THROTTLED")
            retry_count = sum(1 for event in oms_events if event.event_type == "RETRY_SCHEDULED")
            retry_giveup_count = sum(
                1 for event in oms_events if event.event_type == "RETRY_GIVEUP"
            )
            quality_flags = {
                "stale_data": data_age_sec > settings.stage7_max_data_age_sec,
                "missing_mark_price": missing_mark_price_count > 0,
                "spread_spike": spread_bps >= Decimal(str(settings.stage7_spread_spike_bps)),
                "throttled": throttled_events > 0,
                "retry_scheduled": retry_count > 0,
                "retry_giveup": retry_giveup_count > 0,
                "blocked_exchange_writes": bool(settings.kill_switch),
                "simulated_execution": is_backtest_simulation,
            }
            alert_flags = {
                "drawdown_breach": snapshot.max_drawdown >= runtime.stage7_max_drawdown_pct,
                "reject_spike": oms_rejected >= settings.stage7_reject_spike_threshold,
                "missing_data": missing_mark_price_count > 0,
                "throttled": throttled_events > 0,
                "retry_excess": retry_count >= settings.stage7_retry_alert_threshold,
                "retry_giveup": retry_giveup_count > 0,
            }
            no_trades_reason = None
            if planned_count <= 0:
                no_trades_reason = "NO_TRADE_PLANNING"
            elif final_risk_mode == Mode.OBSERVE_ONLY:
                no_trades_reason = "MODE_OBSERVE_ONLY"
            elif settings.kill_switch and not is_backtest_simulation:
                no_trades_reason = "KILL_SWITCH"
            elif oms_filled <= 0:
                no_trades_reason = "NO_FILLS"

            planning_enabled = planned_count > 0
            planning_disabled_reason = _resolve_planning_disabled_reason(
                planning_enabled=planning_enabled,
                selected_universe=universe_result.selected_symbols,
                mark_prices=mark_prices,
                notional_cap_try_per_cycle=Decimal(str(runtime.notional_cap_try_per_cycle)),
                order_intents=order_intents,
            )
            if (
                planning_disabled_reason
                and planning_disabled_reason not in _PLANNING_DISABLED_REASONS
            ):
                planning_disabled_reason = "UNKNOWN"

            skip_reasons: dict[str, int] = {}
            for intent in order_intents:
                if not intent.skipped:
                    continue
                reason = str(intent.skip_reason or "unknown")
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

            planning_diagnostics = {
                "planning_enabled": planning_enabled,
                "planning_disabled_reason": planning_disabled_reason,
                "is_backtest_replay": is_backtest_simulation,
                "dry_run": bool(settings.dry_run),
                "kill_switch": bool(settings.kill_switch),
                "strategy_enabled": bool(settings.stage7_enabled),
                "selected_universe_count": len(universe_result.selected_symbols),
                "mark_prices_count": len(mark_prices),
                "notional_cap_try_per_cycle": str(runtime.notional_cap_try_per_cycle),
                "portfolio_actions_count": len(portfolio_plan.actions),
                "planned_intents_count": planned_count,
                "skipped_intents_count": skipped_count,
                "skip_reasons": dict(sorted(skip_reasons.items())),
                "risk_rejections_count": int(
                    sum(count for key, count in skip_reasons.items() if key.startswith("risk_"))
                ),
            }
            logger.info(
                "stage7_planning_diagnostics",
                extra={"extra": {"cycle_id": cycle_id, **planning_diagnostics}},
            )

            run_metrics_base = {
                "ts": now.isoformat(),
                "run_id": run_id,
                "mode_base": dump_risk_mode(base_risk_mode),
                "mode_final": dump_risk_mode(final_risk_mode),
                "universe_size": len(universe_result.selected_symbols),
                "intents_planned_count": planned_count,
                "intents_skipped_count": skipped_count,
                "oms_submitted_count": oms_submitted,
                "oms_filled_count": oms_filled,
                "oms_rejected_count": oms_rejected,
                "oms_canceled_count": oms_canceled,
                "fills_written_count": fills_written_count,
                "fills_applied_count": fills_applied_count,
                "ledger_events_inserted": ledger_events_inserted,
                "positions_updated_count": positions_updated_count,
                "events_appended": len(oms_events),
                "events_ignored": 0,
                "equity_try": snapshot.equity_try,
                "gross_pnl_try": snapshot.gross_pnl_try,
                "net_pnl_try": snapshot.net_pnl_try,
                "fees_try": snapshot.fees_try,
                "slippage_try": snapshot.slippage_try,
                "max_drawdown_ratio": snapshot.max_drawdown,
                "max_drawdown_pct": snapshot.max_drawdown * Decimal("100"),
                "turnover_try": snapshot.turnover_try,
                "missing_mark_price_count": missing_mark_price_count,
                "oms_throttled_count": throttled_events,
                "retry_count": retry_count,
                "retry_giveup_count": retry_giveup_count,
                "quality_flags": quality_flags,
                "alert_flags": alert_flags,
                "no_trades_reason": no_trades_reason,
                "no_metrics_reason": (
                    "NO_TRADES" if planned_count == 0 and oms_submitted == 0 else None
                ),
            }
            collector.start_timer("persist")
            try:
                state_store.save_stage7_cycle(
                    cycle_id=cycle_id,
                    ts=now,
                    selected_universe=universe_result.selected_symbols,
                    universe_scores=[
                        {
                            "symbol": item.symbol,
                            "total_score": str(item.total_score),
                            "breakdown": item.breakdown,
                        }
                        for item in universe_result.scored[: max(0, runtime.stage7_universe_size)]
                    ],
                    intents_summary={
                        "order_decisions_total": len(actions),
                        "orders_simulated": len(oms_orders),
                        "order_intents_total": len(order_intents),
                        "order_intents_planned": planned_count,
                        "order_intents_skipped": skipped_count,
                        "rules_stats": rules_stats,
                        "planning_diagnostics": planning_diagnostics,
                        "events_total": len(oms_events),
                        "oms_summary": {
                            "orders_total": len(oms_orders),
                            "orders_submitted": sum(
                                1 for o in oms_orders if o.status.value == "SUBMITTED"
                            ),
                            "orders_acked": sum(1 for o in oms_orders if o.status.value == "ACKED"),
                            "orders_partially_filled": sum(
                                1 for o in oms_orders if o.status.value == "PARTIALLY_FILLED"
                            ),
                            "orders_filled": sum(
                                1 for o in oms_orders if o.status.value == "FILLED"
                            ),
                            "orders_rejected": sum(
                                1 for o in oms_orders if o.status.value == "REJECTED"
                            ),
                            "orders_canceled": sum(
                                1 for o in oms_orders if o.status.value == "CANCELED"
                            ),
                        },
                    },
                    mode_payload=mode_payload,
                    order_decisions=actions,
                    portfolio_plan=portfolio_plan.to_dict(),
                    order_intents=order_intents,
                    order_intents_trace=[
                        {
                            "client_order_id": intent.client_order_id,
                            "symbol": intent.symbol,
                            "side": intent.side,
                            "skipped": intent.skipped,
                            "skip_reason": intent.skip_reason,
                        }
                        for intent in order_intents
                    ],
                    ledger_metrics={
                        "gross_pnl_try": snapshot.gross_pnl_try,
                        "realized_pnl_try": snapshot.realized_pnl_try,
                        "unrealized_pnl_try": snapshot.unrealized_pnl_try,
                        "net_pnl_try": snapshot.net_pnl_try,
                        "fees_try": snapshot.fees_try,
                        "slippage_try": snapshot.slippage_try,
                        "turnover_try": snapshot.turnover_try,
                        "equity_try": snapshot.equity_try,
                        "max_drawdown_ratio": snapshot.max_drawdown,
                        "max_drawdown": snapshot.max_drawdown,
                    },
                    risk_decision=stage7_risk_decision,
                    active_param_version=(
                        active_params.version if active_params is not None else 0
                    ),
                )
            finally:
                collector.stop_timer("persist")
            collector.stop_timer("cycle_total")
            finalized = collector.finalize()
            run_metrics: dict[str, object] = {
                **run_metrics_base,
                "latency_ms_total": _coerce_int(finalized.get("latency_ms_total", 0)),
                "selection_ms": _coerce_int(finalized.get("selection_ms", 0)),
                "planning_ms": _coerce_int(finalized.get("planning_ms", 0)),
                "intents_ms": _coerce_int(finalized.get("intents_ms", 0)),
                "oms_ms": _coerce_int(finalized.get("oms_ms", 0)),
                "ledger_ms": _coerce_int(finalized.get("ledger_ms", 0)),
                "persist_ms": _coerce_int(finalized.get("persist_ms", 0)),
                "cycle_total_ms": _coerce_int(finalized.get("cycle_total_ms", 0)),
            }
            state_store.save_stage7_run_metrics(cycle_id, run_metrics)
            observe_histogram(
                "bot_cycle_latency_ms",
                float(run_metrics.get("cycle_total_ms", 0) or 0),
                labels={"process_role": process_role, "mode_final": final_risk_mode.value},
            )
            set_gauge(
                "bot_killswitch_enabled",
                1 if bool(settings.kill_switch) else 0,
                labels={"process_role": process_role},
            )
            if enable_adaptation:
                param_change = adaptation_service.evaluate_and_apply(
                    state_store=state_store, settings=runtime, now_utc=now
                )
                eligible = param_change is not None
                eval_reason = "no_metrics"
                num_changes = 0
                if param_change is not None:
                    eval_reason = param_change.reason
                    num_changes = len(param_change.changes)
                logger.info(
                    "stage7_adaptation_eval",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "eligible": eligible,
                            "reason": eval_reason,
                            "num_changes": num_changes,
                        }
                    },
                )
                if param_change is not None:
                    for key, values in param_change.changes.items():
                        logger.info(
                            "stage7_param_change_applied",
                            extra={
                                "extra": {
                                    "cycle_id": cycle_id,
                                    "key": key,
                                    "old": values.get("old"),
                                    "new": values.get("new"),
                                    "reason": param_change.reason,
                                    "bounds": "applied",
                                }
                            },
                        )
                active_after_eval = state_store.get_active_stage7_params(
                    settings=runtime,
                    now_utc=now,
                )
                state_store.update_stage7_cycle_adaptation_metadata(
                    cycle_id=cycle_id,
                    active_param_version=active_after_eval.version,
                    param_change=param_change,
                )
            state_store.set_last_stage7_cycle_id(cycle_id)

        logger.info(
            "stage7_cycle_financials",
            extra={
                "extra": {
                    "cycle_id": cycle_id,
                    "run_id": run_id,
                    "cash_try": str(snapshot.cash_try),
                    "mtm_try": str(snapshot.position_mtm_try),
                    "realized_pnl_try": str(snapshot.realized_pnl_try),
                    "unrealized_pnl_try": str(snapshot.unrealized_pnl_try),
                    "fees_try": str(snapshot.fees_try),
                    "slippage_try": str(snapshot.slippage_try),
                    "equity_try": str(snapshot.equity_try),
                    "drawdown_ratio": str(snapshot.max_drawdown),
                    "turnover_try": str(snapshot.turnover_try),
                    "sources": {
                        "cash_try": "balances_snapshot.free_quote",
                        "mtm_try": "ledger_open_lots_marked_to_market",
                        "pnl": "ledger_events_fifo",
                        "fees_try": "ledger_fee_events",
                        "slippage_try": "simulated_fill_slippage",
                    },
                }
            },
        )
        snapshot_fn = getattr(exchange, "health_snapshot", None)
        api_snapshot = snapshot_fn() if callable(snapshot_fn) else {}
        breaker_open = bool((api_snapshot or {}).get("breaker_open", False))
        backoff_seconds = float((api_snapshot or {}).get("recommended_sleep_seconds", 0.0) or 0.0)
        api_health = (
            "degraded" if bool((api_snapshot or {}).get("degraded") or breaker_open) else "healthy"
        )
        consecutive_errors = state_store.get_consecutive_critical_errors(process_role)
        kill_enabled, kill_reason, _kill_until = state_store.get_kill_switch(process_role)
        cycle_summary = {
            "cycle_id": cycle_id,
            "role": process_role,
            "dry_run": bool(settings.dry_run),
            "mode": final_risk_mode.value,
            "api_health": api_health,
            "breaker_open": breaker_open,
            "backoff_seconds": backoff_seconds,
            "intents_total": len(order_intents),
            "intents_rejected_precheck": skipped_count,
            "intents_rejected_risk": len([i for i in order_intents if i.skipped]),
            "orders_submitted": oms_submitted,
            "orders_canceled": oms_canceled,
            "would_submit_orders": oms_submitted if bool(settings.dry_run) else 0,
            "would_cancel_orders": oms_canceled if bool(settings.dry_run) else 0,
            "pnl_realized_try": str(snapshot.realized_pnl_try),
            "pnl_unrealized_try": str(snapshot.unrealized_pnl_try),
            "fees_try": str(snapshot.fees_try),
            "consecutive_critical_errors": consecutive_errors,
            "kill_switch_state": "killed" if kill_enabled else "normal",
            "latency_ms": _coerce_int(finalized.get("cycle_total_ms", 0)),
        }
        state_store.set_runtime_state(
            f"cycle_summary:{process_role}", json.dumps(cycle_summary, sort_keys=True)
        )
        state_store.set_runtime_counter(
            f"orders_submitted:{process_role}", int(cycle_summary["orders_submitted"])
        )
        logger.info("cycle_summary", extra={"extra": cycle_summary})
        logger.info(
            "stage7_cycle_end",
            extra={"extra": {"cycle_id": cycle_id, "run_id": run_id, "kill_reason": kill_reason}},
        )
        return stage4_result

    def _build_stage7_order_intents(
        self,
        *,
        cycle_id: str,
        now: datetime,
        runtime: Settings,
        universe_service: UniverseSelectionService,
        base_client: object,
        mark_prices: dict[str, Decimal],
        balances: list[Balance],
        open_orders: list[object],
        final_mode: Mode,
        rules_service: ExchangeRulesService,
        rules_unavailable: dict[str, str],
        selected_universe: list[str],
        policy_service: PortfolioPolicyService,
        order_builder: OrderBuilderService,
    ) -> tuple[PortfolioPlan, list[OrderIntent], str]:
        if not getattr(runtime, "stage7_use_planning_kernel", True):
            portfolio_plan, order_intents = self._build_stage7_order_intents_legacy(
                cycle_id=cycle_id,
                now=now,
                runtime=runtime,
                selected_universe=selected_universe,
                mark_prices=mark_prices,
                balances=balances,
                final_mode=final_mode,
                rules_service=rules_service,
                rules_unavailable=rules_unavailable,
                policy_service=policy_service,
                order_builder=order_builder,
            )
            return portfolio_plan, order_intents, "legacy"

        context = build_stage7_planning_context(
            cycle_id=cycle_id,
            now_utc=now,
            mark_prices=mark_prices,
            balances=balances,
            open_orders=normalize_stage4_open_orders(open_orders),
            quote_ccy=runtime.stage7_universe_quote_ccy,
        )
        universe_adapter = Stage7UniverseSelectorAdapter(
            service=universe_service,
            exchange=base_client,
            settings=runtime,
            now_utc=now,
            _cached=[normalize_symbol(item) for item in selected_universe],
        )
        strategy_adapter = Stage7PortfolioStrategyAdapter(
            policy_service=policy_service,
            settings=runtime,
            now_utc=now,
            final_mode=final_mode,
        )
        allocator_adapter = Stage7PassThroughAllocator()
        order_intent_builder_adapter = Stage7OrderIntentBuilderAdapter(
            order_builder=order_builder,
            strategy_adapter=strategy_adapter,
            settings=runtime,
            final_mode=final_mode,
            now_utc=now,
            rules=rules_service,
            rules_unavailable=rules_unavailable,
        )
        kernel = PlanningKernel(
            universe_selector=universe_adapter,
            strategy_engine=strategy_adapter,
            allocator=allocator_adapter,
            order_intent_builder=order_intent_builder_adapter,
        )
        plan = kernel.plan(context)
        return strategy_adapter.last_portfolio_plan, list(plan.order_intents), "kernel"

    def _build_stage7_order_intents_legacy(
        self,
        *,
        cycle_id: str,
        now: datetime,
        runtime: Settings,
        selected_universe: list[str],
        mark_prices: dict[str, Decimal],
        balances: list[Balance],
        final_mode: Mode,
        rules_service: ExchangeRulesService,
        rules_unavailable: dict[str, str],
        policy_service: PortfolioPolicyService,
        order_builder: OrderBuilderService,
    ) -> tuple[PortfolioPlan, list[OrderIntent]]:
        portfolio_plan = policy_service.build_plan(
            universe=selected_universe,
            mark_prices_try=mark_prices,
            balances=balances,
            settings=runtime,
            now_utc=now,
            final_mode=final_mode,
        )
        order_intents = order_builder.build_intents(
            cycle_id=cycle_id,
            plan=portfolio_plan,
            mark_prices_try=mark_prices,
            rules=rules_service,
            settings=runtime,
            final_mode=final_mode,
            now_utc=now,
            rules_unavailable=rules_unavailable,
        )
        return portfolio_plan, order_intents

    def consume_shared_plan(self, plan: Plan, execution: ExecutionPort) -> list[str]:
        """Submit shared Plan order intents through a stage-specific execution adapter."""

        return Stage7PlanConsumer(execution=execution).consume(plan)
