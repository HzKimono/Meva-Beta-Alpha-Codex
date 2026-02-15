from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from btcbot.config import Settings
from btcbot.domain.accounting import Position, TradeFill
from btcbot.domain.anomalies import combine_modes
from btcbot.domain.ledger import LedgerEvent, LedgerEventType, LedgerState, apply_events
from btcbot.domain.models import Balance, normalize_symbol
from btcbot.domain.models import OrderSide as DomainOrderSide
from btcbot.domain.risk_budget import Mode
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType
from btcbot.logging_context import with_cycle_context
from btcbot.services.adaptation_service import AdaptationService
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.exposure_tracker import ExposureTracker
from btcbot.services.ledger_service import LedgerService
from btcbot.services.metrics_collector import MetricsCollector
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.order_builder_service import OrderBuilderService
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.stage7_risk_budget_service import Stage7RiskBudgetService, Stage7RiskInputs
from btcbot.services.state_store import StateStore
from btcbot.services.universe_selection_service import _BPS, UniverseSelectionService

logger = logging.getLogger(__name__)


def _deterministic_fill_id(cycle_id: str, client_order_id: str, symbol: str, side: str) -> str:
    digest = sha256(f"{cycle_id}|{client_order_id}|{symbol}|{side}".encode()).hexdigest()[:16]
    return f"s7f:{digest}"


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
        rules_service = ExchangeRulesService(
            base_client,
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

            risk_inputs = Stage7RiskInputs(
                max_drawdown_pct=latest_metrics["max_drawdown_ratio"],
                daily_pnl_try=latest_metrics["net_pnl_try"],
                consecutive_loss_streak=0,
                market_data_age_sec=data_age_sec,
                observed_spread_bps=spread_bps,
                quote_volume_try=quote_volume_try,
                exposure_snapshot=exposure_snapshot,
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

            rules_stats = {
                "rules_fallback_used_count": len(rules_symbols_fallback),
                "rules_invalid_metadata_count": len(rules_symbols_invalid_metadata),
                "rules_missing_count": len(rules_symbols_missing),
                "rules_error_count": len(rules_symbols_error),
                "rules_symbols_fallback": sorted(rules_symbols_fallback),
                "rules_symbols_invalid_metadata": sorted(rules_symbols_invalid_metadata),
                "rules_symbols_missing": sorted(rules_symbols_missing),
                "rules_symbols_error": sorted(rules_symbols_error),
                "rules_unavailable_details": dict(sorted(rules_unavailable_details.items())),
            }

            base_mode = state_store.get_latest_risk_mode()
            final_mode = combine_modes(base_mode, None)
            stage7_mode = Mode(stage7_risk_decision.mode.value)
            final_mode = combine_modes(final_mode, stage7_mode)
            invalid_policy = settings.stage7_rules_invalid_metadata_policy
            if invalid_policy == "observe_only_cycle" and (
                rules_stats["rules_invalid_metadata_count"] > 0
                or rules_stats["rules_missing_count"] > 0
                or rules_stats["rules_error_count"] > 0
            ):
                final_mode = Mode.OBSERVE_ONLY

            mode_payload = {
                "base_mode": base_mode.value,
                "override_mode": None,
                "final_mode": final_mode.value,
                "risk_mode": stage7_risk_decision.mode.value,
                "risk_reasons": stage7_risk_decision.reasons,
                "risk_cooldown_until": (
                    stage7_risk_decision.cooldown_until.isoformat()
                    if stage7_risk_decision.cooldown_until
                    else None
                ),
                "risk_inputs_hash": stage7_risk_decision.inputs_hash,
            }

            collector.start_timer("planning")
            portfolio_plan = policy_service.build_plan(
                universe=universe_result.selected_symbols,
                mark_prices_try=mark_prices,
                balances=balances,
                settings=runtime,
                now_utc=now,
                final_mode=final_mode,
            )
            collector.stop_timer("planning")

            collector.start_timer("intents")
            try:
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
            finally:
                collector.stop_timer("intents")

            actions: list[dict[str, object]] = []
            slippage_try = Decimal("0")
            oms_orders = []
            oms_events = []
            fills_written_count = 0
            fills_applied_count = 0
            ledger_events_inserted = 0
            positions_updated_count = 0

            if final_mode != Mode.OBSERVE_ONLY:
                filtered_actions: list[LifecycleAction] = []
                skipped_actions: list[dict[str, object]] = []
                for action in lifecycle_actions:
                    normalized_symbol = normalize_symbol(action.symbol)
                    if final_mode == Mode.REDUCE_RISK_ONLY and action.side.upper() != "SELL":
                        skipped_actions.append(
                            {
                                "symbol": normalized_symbol,
                                "side": action.side,
                                "qty": str(action.qty),
                                "status": "skipped",
                                "reason": "mode_reduce_risk_only",
                            }
                        )
                        continue
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

                collector.start_timer("oms")
                oms_service = OMSService()
                market_simulator = Stage7MarketSimulator(mark_prices)
                reconciled_orders, reconciled_events = oms_service.reconcile_open_orders(
                    cycle_id=cycle_id,
                    now_utc=now,
                    state_store=state_store,
                    settings=runtime,
                    market_sim=market_simulator,
                )
                planned_intents = [intent for intent in order_intents if not intent.skipped]
                oms_orders, oms_events = oms_service.process_intents(
                    cycle_id=cycle_id,
                    now_utc=now,
                    intents=planned_intents,
                    market_sim=market_simulator,
                    state_store=state_store,
                    settings=runtime,
                    cancel_requests=[],
                )
                oms_orders = [*reconciled_orders, *oms_orders]
                oms_events = [*reconciled_events, *oms_events]
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

            ledger_state = apply_events(LedgerState(), state_store.load_ledger_events())
            for symbol in sorted(ledger_state.symbols):
                symbol_state = ledger_state.symbols[symbol]
                qty = sum((lot.qty for lot in symbol_state.lots), Decimal("0"))
                notional = sum((lot.qty * lot.unit_cost for lot in symbol_state.lots), Decimal("0"))
                avg_cost = (notional / qty) if qty > 0 else Decimal("0")
                mark = mark_prices.get(symbol, avg_cost)
                unrealized = sum((mark - lot.unit_cost) * lot.qty for lot in symbol_state.lots)
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
            }
            alert_flags = {
                "drawdown_breach": snapshot.max_drawdown >= runtime.stage7_max_drawdown_pct,
                "reject_spike": oms_rejected >= settings.stage7_reject_spike_threshold,
                "missing_data": missing_mark_price_count > 0,
                "throttled": throttled_events > 0,
                "retry_excess": retry_count >= settings.stage7_retry_alert_threshold,
                "retry_giveup": retry_giveup_count > 0,
            }
            run_metrics_base = {
                "ts": now.isoformat(),
                "run_id": run_id,
                "mode_base": base_mode.value,
                "mode_final": final_mode.value,
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
            run_metrics = {
                **run_metrics_base,
                "latency_ms_total": int(finalized.get("latency_ms_total", 0)),
                "selection_ms": int(finalized.get("selection_ms", 0)),
                "planning_ms": int(finalized.get("planning_ms", 0)),
                "intents_ms": int(finalized.get("intents_ms", 0)),
                "oms_ms": int(finalized.get("oms_ms", 0)),
                "ledger_ms": int(finalized.get("ledger_ms", 0)),
                "persist_ms": int(finalized.get("persist_ms", 0)),
                "cycle_total_ms": int(finalized.get("cycle_total_ms", 0)),
            }
            state_store.save_stage7_run_metrics(cycle_id, run_metrics)
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
        logger.info("stage7_cycle_end", extra={"extra": {"cycle_id": cycle_id, "run_id": run_id}})
        return stage4_result
