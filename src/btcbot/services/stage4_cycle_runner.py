from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from btcbot.adapters.action_to_order import build_exchange_rules
from btcbot.adapters.btcturk_http import ConfigurationError
from btcbot.agent.audit import AgentAuditTrail
from btcbot.agent.contracts import AgentContext, AgentDecision, DecisionAction, DecisionRationale
from btcbot.agent.guardrails import SafetyGuard
from btcbot.agent.policy import FallbackPolicy, LlmPolicy, PromptBuilder, RuleBasedPolicy
from btcbot.config import Settings
from btcbot.domain.anomalies import AnomalyCode, combine_modes, decide_degrade
from btcbot.domain.models import PairInfo, normalize_symbol
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.risk_budget import Mode, RiskLimits
from btcbot.domain.stage4 import (
    LifecycleAction,
    LifecycleActionType,
    Order,
    Position,
    Quantizer,
)
from btcbot.domain.strategy_core import OrderBookSummary, PositionSummary
from btcbot.observability_decisions import emit_decision
from btcbot.planning_kernel import ExecutionPort, Plan
from btcbot.services import metrics_service
from btcbot.services.account_snapshot_service import AccountSnapshotService
from btcbot.services.accounting_service_stage4 import AccountingService
from btcbot.services.anomaly_detector_service import AnomalyDetectorConfig, AnomalyDetectorService
from btcbot.services.decision_pipeline_service import DecisionPipelineService
from btcbot.services.dynamic_universe_service import DynamicUniverseService
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.execution_service_stage4 import ExecutionService
from btcbot.services.ledger_service import LedgerService
from btcbot.services.metrics_service import CycleMetrics
from btcbot.services.order_lifecycle_service import OrderLifecycleService
from btcbot.services.planning_kernel_adapters import Stage4PlanConsumer
from btcbot.services.price_conversion_service import MarkPriceConverter
from btcbot.services.reconcile_service import ReconcileService
from btcbot.services.risk_budget_service import CapitalPolicyError, RiskBudgetService
from btcbot.services.risk_policy import RiskPolicy
from btcbot.services.stage4_planning_kernel_integration import build_stage4_kernel_plan
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)


class _UnavailableLlmClient:
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        raise RuntimeError("llm client not configured")


@dataclass(frozen=True)
class MarketSnapshot:
    mark_prices: dict[str, Decimal]
    orderbooks: dict[str, tuple[Decimal, Decimal]]
    anomalies: set[str]
    spreads_bps: dict[str, Decimal]
    age_seconds_by_symbol: dict[str, Decimal]
    fetched_at_by_symbol: dict[str, datetime | None]
    max_data_age_seconds: Decimal


class Stage4ConfigurationError(RuntimeError):
    pass


class Stage4ExchangeError(RuntimeError):
    pass


class Stage4InvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class Stage4CycleRunner:
    command: str = "stage4-run"

    @staticmethod
    def norm(symbol: str) -> str:
        return normalize_symbol(symbol)

    def run_one_cycle(self, settings: Settings) -> int:
        exchange = build_exchange_stage4(settings, dry_run=settings.dry_run)
        live_mode = settings.is_live_trading_enabled() and not settings.dry_run
        state_store = StateStore(db_path=settings.state_db_path)
        if live_mode and state_store.get_latest_stage7_ledger_metrics() is not None:
            logger.warning(
                "stage4_live_stage7_data_present",
                extra={
                    "extra": {
                        "state_db_path": settings.state_db_path,
                        "reason_code": "db_mixed_stage4_stage7",
                    }
                },
            )
        cycle_id = uuid4().hex
        cycle_now = datetime.now(UTC)
        cycle_started_at = cycle_now
        pair_info = self._resolve_pair_info(exchange) or []
        active_symbols = [self.norm(symbol) for symbol in settings.symbols]
        aggressive_scores: dict[str, Decimal] | None = None
        if settings.dynamic_universe_enabled:
            selection = DynamicUniverseService().select(
                exchange=exchange,
                state_store=state_store,
                settings=settings,
                now_utc=cycle_now,
                cycle_id=cycle_id,
            )
            if selection.selected_symbols:
                active_symbols = [self.norm(symbol) for symbol in selection.selected_symbols]
                aggressive_scores = {
                    self.norm(symbol): Decimal(str(score))
                    for symbol, score in selection.scores.items()
                }

        envelope = {
            "cycle_id": cycle_id,
            "command": self.command,
            "dry_run": settings.dry_run,
            "live_mode": live_mode,
            "symbols": sorted(self.norm(symbol) for symbol in active_symbols),
            "timestamp_utc": cycle_now.isoformat(),
        }

        try:
            rules_service = ExchangeRulesService(
                exchange, cache_ttl_sec=settings.rules_cache_ttl_sec
            )
            accounting_service = AccountingService(
                exchange=exchange,
                state_store=state_store,
                lookback_minutes=settings.fills_poll_lookback_minutes,
            )
            lifecycle_service = OrderLifecycleService(stale_after_sec=settings.ttl_seconds)
            reconcile_service = ReconcileService()
            risk_policy = RiskPolicy(
                max_open_orders=settings.max_open_orders,
                max_position_notional_try=settings.max_position_notional_try,
                max_daily_loss_try=settings.max_daily_loss_try,
                max_drawdown_pct=settings.max_drawdown_pct,
                fee_bps_taker=settings.fee_bps_taker,
                slippage_bps_buffer=settings.slippage_bps_buffer,
                min_profit_bps=Decimal(str(settings.min_profit_bps)),
            )
            risk_budget_service = RiskBudgetService(state_store=state_store)
            execution_service = ExecutionService(
                exchange=exchange,
                state_store=state_store,
                settings=settings,
                rules_service=rules_service,
            )
            decision_pipeline = DecisionPipelineService(settings=settings)
            anomaly_detector = AnomalyDetectorService(
                config=AnomalyDetectorConfig(
                    stale_market_data_seconds=settings.stale_market_data_seconds,
                    reject_spike_threshold=settings.reject_spike_threshold,
                    latency_spike_ms=settings.latency_spike_ms,
                    cursor_stall_cycles=settings.cursor_stall_cycles,
                    pnl_divergence_try_warn=settings.pnl_divergence_try_warn,
                    pnl_divergence_try_error=settings.pnl_divergence_try_error,
                    clock_skew_seconds_threshold=settings.clock_skew_seconds_threshold,
                ),
            )

            market_snapshot = self._resolve_market_snapshot(
                exchange,
                active_symbols,
                cycle_now=cycle_now,
            )
            mark_prices = market_snapshot.mark_prices
            stale_symbols = {
                symbol
                for symbol, age in market_snapshot.age_seconds_by_symbol.items()
                if age > Decimal(str(settings.stale_market_data_seconds))
            }
            if stale_symbols:
                logger.warning(
                    "stale_market_data_age_exceeded",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "symbols": sorted(stale_symbols),
                            "max_age_seconds": str(market_snapshot.max_data_age_seconds),
                            "threshold_seconds": str(settings.stale_market_data_seconds),
                        }
                    },
                )
            mark_price_errors = market_snapshot.anomalies | stale_symbols
            snapshot_service = AccountSnapshotService(exchange=exchange)
            account_snapshot = snapshot_service.build_snapshot(
                symbols=active_symbols,
                fallback_try_cash=Decimal(str(settings.dry_run_try_balance)),
            )
            try_cash = account_snapshot.cash_try
            state_store.save_account_snapshot(cycle_id=cycle_id, snapshot=account_snapshot)
            holdings_summary = {
                asset: str(item.total)
                for asset, item in account_snapshot.holdings.items()
                if item.total > Decimal("0")
            }
            logger.info(
                "stage4_account_snapshot",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "cash_try": str(account_snapshot.cash_try),
                        "equity_try": str(account_snapshot.total_equity_try),
                        "holdings": holdings_summary,
                        "flags": list(account_snapshot.flags),
                        "source_endpoints": list(account_snapshot.source_endpoints),
                    }
                },
            )

            exchange_open_orders: list[Order] = []
            open_order_failures = 0
            failed_symbols: set[str] = set(mark_price_errors)
            for symbol in active_symbols:
                normalized = self.norm(symbol)
                try:
                    exchange_open_orders.extend(exchange.list_open_orders(symbol))
                except Exception as exc:  # noqa: BLE001
                    open_order_failures += 1
                    failed_symbols.add(normalized)
                    logger.warning(
                        "stage4_open_orders_fetch_failed",
                        extra={"extra": {"symbol": normalized, "error_type": type(exc).__name__}},
                    )

            db_open_orders = state_store.list_stage4_open_orders()
            reconcile_result = reconcile_service.resolve(
                exchange_open_orders=exchange_open_orders,
                db_open_orders=db_open_orders,
            )
            ledger_service = LedgerService(state_store=state_store, logger=logger)
            for order in reconcile_result.import_external:
                state_store.import_stage4_external_order(order)
            for client_order_id, exchange_order_id in reconcile_result.enrich_exchange_ids:
                state_store.update_stage4_order_exchange_id(client_order_id, exchange_order_id)
            for client_order_id in reconcile_result.mark_unknown_closed:
                state_store.mark_stage4_unknown_closed(client_order_id)

            fills = []
            fills_fetched = 0
            fills_failures = 0
            cursor_before = {
                self.norm(symbol): state_store.get_cursor(self._fills_cursor_key(symbol))
                for symbol in active_symbols
            }
            cursor_after_by_symbol: dict[str, str] = {}
            cursor_diag: dict[str, dict[str, object]] = {
                self.norm(symbol): {
                    "cursor_before": cursor_before.get(self.norm(symbol)),
                    "ingested_count": 0,
                    "last_seen_trade_id": None,
                    "last_seen_timestamp": None,
                    "cursor_written": False,
                }
                for symbol in active_symbols
            }
            for symbol in active_symbols:
                normalized = self.norm(symbol)
                try:
                    fetched = accounting_service.fetch_new_fills(symbol)
                    fills.extend(fetched.fills)
                    fills_fetched += len(fetched.fills)
                    cursor_diag[normalized]["ingested_count"] = len(fetched.fills)
                    cursor_diag[normalized]["last_seen_trade_id"] = getattr(
                        fetched, "last_seen_fill_id", None
                    )
                    cursor_diag[normalized]["last_seen_timestamp"] = getattr(
                        fetched, "last_seen_ts_ms", None
                    )
                    if fetched.cursor_after is not None:
                        cursor_after_by_symbol[normalized] = fetched.cursor_after
                except Exception as exc:  # noqa: BLE001
                    fills_failures += 1
                    failed_symbols.add(normalized)
                    logger.warning(
                        "stage4_fills_fetch_failed",
                        extra={"extra": {"symbol": normalized, "error_type": type(exc).__name__}},
                    )

            try:
                with state_store.transaction():
                    ledger_ingest = ledger_service.ingest_exchange_updates(fills)
                    snapshot = accounting_service.apply_fills(
                        fills, mark_prices=mark_prices, try_cash=try_cash
                    )
                    for symbol, cursor_after in cursor_after_by_symbol.items():
                        if symbol in failed_symbols:
                            continue
                        state_store.set_cursor(self._fills_cursor_key(symbol), cursor_after)
                        cursor_diag[symbol]["cursor_written"] = True
            except Exception as exc:
                logger.exception(
                    "cycle_failed",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "reason_code": "state_transaction_failed",
                            "error_type": type(exc).__name__,
                        }
                    },
                )
                raise
            cursor_after = {
                self.norm(symbol): state_store.get_cursor(self._fills_cursor_key(symbol))
                for symbol in active_symbols
            }
            for symbol, diag in cursor_diag.items():
                diag["cursor_after"] = cursor_after.get(symbol)
                ingested_count = int(diag.get("ingested_count", 0) or 0)
                deduped_count = 0
                if (
                    ingested_count > 0
                    and ledger_ingest.events_inserted == 0
                    and ledger_ingest.events_ignored >= ingested_count
                ):
                    deduped_count = ingested_count
                diag["deduped_count"] = deduped_count
                diag["persisted_count"] = max(0, ingested_count - deduped_count)
            logger.info(
                "fills_cursor_diagnostics",
                extra={"extra": {"cycle_id": cycle_id, "cursor": cursor_diag}},
            )
            if ledger_ingest.events_ignored > 0:
                logger.info(
                    "ledger_events_deduped",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "ignored": ledger_ingest.events_ignored,
                            "attempted": ledger_ingest.events_attempted,
                            "inserted": ledger_ingest.events_inserted,
                        }
                    },
                )

            fee_converter = MarkPriceConverter(mark_prices)
            pnl_report = ledger_service.report(
                mark_prices=mark_prices,
                cash_try=try_cash,
                price_for_fee_conversion=fee_converter,
            )
            ledger_checkpoint = ledger_service.checkpoint()
            try:
                risk_budget_service.apply_self_financing_checkpoint(
                    cycle_id=cycle_id,
                    realized_pnl_total_try=pnl_report.realized_pnl_total,
                    ledger_event_count=ledger_checkpoint.event_count,
                    ledger_checkpoint_id=ledger_checkpoint.checkpoint_id,
                    seed_trading_capital_try=try_cash,
                )
            except CapitalPolicyError as exc:
                emit_decision(
                    logger,
                    {
                        "cycle_id": cycle_id,
                        "decision_layer": "capital_policy",
                        "reason_code": "capital_error:self_financing_failed",
                        "action": "BLOCK",
                        "scope": "global",
                        "payload": {
                            "checkpoint_id": ledger_checkpoint.checkpoint_id,
                            "ledger_event_count": ledger_checkpoint.event_count,
                            "error": str(exc),
                        },
                    },
                )
                raise Stage4InvariantError(str(exc)) from exc
            current_open_orders = state_store.list_stage4_open_orders()
            positions = state_store.list_stage4_positions()
            positions_by_symbol = {self.norm(position.symbol): position for position in positions}
            planning_engine = "legacy"
            kernel_plan = None
            risk_limits = RiskLimits(
                max_daily_drawdown_try=settings.risk_max_daily_drawdown_try,
                max_drawdown_try=settings.risk_max_drawdown_try,
                max_gross_exposure_try=settings.risk_max_gross_exposure_try,
                max_position_pct=settings.risk_max_position_pct,
                max_order_notional_try=settings.risk_max_order_notional_try,
                min_cash_try=settings.risk_min_cash_try,
                max_fee_try_per_day=settings.risk_max_fee_try_per_day,
            )
            budget_decision, prev_mode, peak_equity, fees_today, risk_day = (
                risk_budget_service.compute_decision(
                    limits=risk_limits,
                    pnl_report=pnl_report,
                    positions=positions,
                    mark_prices=mark_prices,
                    realized_today_try=snapshot.realized_today_try,
                    kill_switch_active=settings.kill_switch,
                )
            )
            budget_notional_multiplier = getattr(
                budget_decision, "position_sizing_multiplier", Decimal("1")
            )
            if settings.kill_switch:
                budget_notional_multiplier = Decimal("0")

            if settings.stage4_use_planning_kernel:
                kernel_result = build_stage4_kernel_plan(
                    settings=settings,
                    cycle_id=cycle_id,
                    now_utc=cycle_now,
                    selected_symbols=[
                        symbol
                        for symbol in active_symbols
                        if self.norm(symbol) not in failed_symbols
                    ],
                    mark_prices=mark_prices,
                    try_cash=try_cash,
                    positions=positions,
                    open_orders=current_open_orders,
                    pair_info=pair_info,
                    live_mode=live_mode,
                    aggressive_scores=aggressive_scores,
                    bootstrap_builder=self._build_intents,
                )
                planning_engine = "kernel"
                kernel_plan = kernel_result.plan
                decision_report = kernel_result.decision_report
                bootstrap_drop_reasons = dict(kernel_result.bootstrap_drop_reasons)
                intents = self._translate_kernel_order_intents(
                    order_intents=list(kernel_result.plan.order_intents),
                    now_utc=cycle_now,
                    live_mode=live_mode,
                )
            else:
                decision_report = decision_pipeline.run_cycle(
                    cycle_id=cycle_id,
                    balances={"TRY": try_cash},
                    positions={
                        symbol: self._to_position_summary(position)
                        for symbol, position in positions_by_symbol.items()
                    },
                    mark_prices=mark_prices,
                    open_orders=current_open_orders,
                    pair_info=pair_info,
                    orderbooks={
                        symbol: OrderBookSummary(best_bid=bid, best_ask=ask)
                        for symbol, (bid, ask) in market_snapshot.orderbooks.items()
                    },
                    bootstrap_enabled=settings.stage4_bootstrap_intents,
                    live_mode=live_mode,
                    preferred_symbols=active_symbols,
                    aggressive_scores=aggressive_scores,
                    budget_notional_multiplier=budget_notional_multiplier,
                )
            planned_payload = [
                {
                    "symbol": item.symbol,
                    "side": item.side,
                    "notional_try": str(item.notional_try),
                    "qty": str(item.qty),
                    "reason": item.rationale,
                }
                for item in decision_report.allocation_actions
            ]
            decisions_payload = [
                {
                    "symbol": item.symbol,
                    "status": item.status,
                    "reason": item.reason,
                    "requested_notional_try": (
                        str(item.requested_notional_try)
                        if item.requested_notional_try is not None
                        else None
                    ),
                }
                for item in decision_report.allocation_decisions
            ]
            logger.info(
                "stage4_allocation_plan",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "try_cash_target": str(decision_report.try_cash_target),
                        "cash_try": str(decision_report.cash_try),
                        "investable_total_try": str(decision_report.investable_total_try),
                        "investable_this_cycle_try": str(decision_report.investable_this_cycle_try),
                        "deploy_budget_try": str(decision_report.deploy_budget_try),
                        "planned_total_try": str(decision_report.planned_total_try),
                        "unused_budget_try": str(decision_report.unused_budget_try),
                        "unused_reason": decision_report.investable_usage_reason,
                        "selected_order_requests": len(decision_report.order_requests),
                        "deferred_order_requests": len(decision_report.deferred_order_requests),
                        "planned": planned_payload,
                        "deferred": [
                            {
                                "symbol": item.symbol,
                                "side": item.side,
                                "qty": str(item.qty),
                                "price": str(item.price),
                                "reason": "max_orders_per_cycle",
                            }
                            for item in decision_report.deferred_order_requests
                        ],
                        "decisions": decisions_payload,
                    }
                },
            )
            state_store.save_allocation_plan(
                cycle_id=cycle_id,
                ts=cycle_now,
                cash_try=decision_report.cash_try,
                try_cash_target=decision_report.try_cash_target,
                investable_total_try=decision_report.investable_total_try,
                investable_this_cycle_try=decision_report.investable_this_cycle_try,
                deploy_budget_try=decision_report.deploy_budget_try,
                planned_total_try=decision_report.planned_total_try,
                unused_budget_try=decision_report.unused_budget_try,
                usage_reason=decision_report.investable_usage_reason,
                plan=planned_payload,
                deferred=[
                    {
                        "symbol": item.symbol,
                        "side": item.side,
                        "qty": str(item.qty),
                        "price": str(item.price),
                        "reason": "max_orders_per_cycle",
                    }
                    for item in decision_report.deferred_order_requests
                ],
                decisions=decisions_payload,
            )
            if not settings.stage4_use_planning_kernel:
                pipeline_orders = [
                    order
                    for order in decision_report.order_requests
                    if self.norm(order.symbol) not in failed_symbols
                ]
                bootstrap_intents, bootstrap_drop_reasons = self._build_intents(
                    cycle_id=cycle_id,
                    min_order_notional_try=Decimal(str(settings.min_order_notional_try)),
                    bootstrap_notional_try=Decimal(str(settings.stage5_bootstrap_notional_try)),
                    max_notional_per_order_try=Decimal(str(settings.max_notional_per_order_try)),
                    symbols=[
                        symbol
                        for symbol in active_symbols
                        if self.norm(symbol) not in failed_symbols
                    ],
                    mark_prices=mark_prices,
                    try_cash=try_cash,
                    open_orders=current_open_orders,
                    live_mode=live_mode,
                    bootstrap_enabled=settings.stage4_bootstrap_intents,
                    pair_info=pair_info,
                    now_utc=cycle_now,
                )
                intents = pipeline_orders or bootstrap_intents
            else:
                bootstrap_intents = []

            intents = self._apply_agent_policy(
                settings=settings,
                state_store=state_store,
                cycle_id=cycle_id,
                cycle_started_at=cycle_started_at,
                cycle_now=cycle_now,
                intents=intents,
                mark_prices=mark_prices,
                market_spreads_bps=market_snapshot.spreads_bps,
                market_data_age_seconds=market_snapshot.max_data_age_seconds,
                positions=positions,
                current_open_orders=current_open_orders,
                snapshot=snapshot,
                live_mode=live_mode,
                failed_symbols=failed_symbols,
                budget_guard_multiplier=budget_notional_multiplier,
            )

            mid_price = next(iter(mark_prices.values()), Decimal("0"))
            lifecycle_plan = lifecycle_service.plan(
                intents, current_open_orders, mid_price=mid_price
            )

            current_position_notional = Decimal("0")
            for position in positions:
                mark = mark_prices.get(self.norm(position.symbol), position.avg_cost_try)
                current_position_notional += position.qty * mark

            safe_actions = [
                action
                for action in lifecycle_plan.actions
                if self.norm(action.symbol) not in failed_symbols
            ]
            accepted_actions, risk_decisions = risk_policy.filter_actions(
                safe_actions,
                open_orders_count=len(current_open_orders),
                current_position_notional_try=current_position_notional,
                pnl=snapshot,
                positions_by_symbol=positions_by_symbol,
            )

            risk_decision = getattr(budget_decision, "risk_decision", budget_decision)
            try:
                risk_budget_service.persist_decision(
                    cycle_id=cycle_id,
                    decision=budget_decision,
                    prev_mode=prev_mode,
                    peak_equity=peak_equity,
                    peak_day=risk_day,
                    fees_today=fees_today,
                    fees_day=risk_day,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "risk_decision_persist_failed",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "reason_code": "risk_decision_persist_failed",
                            "error_type": type(exc).__name__,
                        }
                    },
                )

            degrade_state = state_store.get_degrade_state_current()
            cooldown_until_raw = degrade_state.get("cooldown_until")
            cooldown_until = (
                datetime.fromisoformat(cooldown_until_raw) if cooldown_until_raw else None
            )
            current_override_raw = degrade_state.get("current_override_mode")
            current_override = (
                Mode(current_override_raw)
                if current_override_raw in {m.value for m in Mode}
                else None
            )
            last_reasons = self._safe_json_list(degrade_state.get("last_reasons_json"), default=[])
            prev_warn_codes = self._parse_warn_codes(
                self._safe_json_list(degrade_state.get("last_warn_codes_json"), default=[])
            )
            prev_warn_window_count = int(degrade_state.get("warn_window_count", "0"))
            prev_stall_cycles = self._safe_json_dict_int(
                degrade_state.get("cursor_stall_cycles_json"), default={}
            )
            prev_reject_count = int(degrade_state.get("last_reject_count", "0"))

            cursor_stall_by_symbol: dict[str, int] = {}
            if not settings.dry_run:
                for symbol in cursor_before:
                    prev = int(prev_stall_cycles.get(symbol, 0))
                    before_value = cursor_before.get(symbol)
                    after_value = cursor_after.get(symbol)
                    dedupe_only_cycle = (
                        int(cursor_diag.get(symbol, {}).get("ingested_count", 0) or 0) > 0
                        and int(cursor_diag.get(symbol, {}).get("persisted_count", 0) or 0) == 0
                        and ledger_ingest.events_inserted == 0
                        and ledger_ingest.events_ignored > 0
                    )
                    if before_value is not None and before_value == after_value and not dedupe_only_cycle:
                        cursor_stall_by_symbol[symbol] = prev + 1
                    else:
                        cursor_stall_by_symbol[symbol] = 0

            cycle_observed_at = datetime.now(UTC)
            cycle_duration_ms = int((cycle_observed_at - cycle_started_at).total_seconds() * 1000)
            anomalies = anomaly_detector.detect(
                market_data_age_seconds={
                    k: float(v) for k, v in market_snapshot.age_seconds_by_symbol.items()
                },
                reject_count=prev_reject_count,
                cycle_duration_ms=cycle_duration_ms,
                cursor_stall_by_symbol=cursor_stall_by_symbol,
                pnl_snapshot=snapshot,
                pnl_report=pnl_report,
            )
            warn_codes = settings.parsed_degrade_warn_codes()
            current_warn_codes = {
                event.code
                for event in anomalies
                if event.severity == "WARN" and event.code in warn_codes
            }
            has_warn = bool(current_warn_codes)
            warn_window_count = (prev_warn_window_count + 1) if has_warn else 0
            recent_warn_codes = current_warn_codes or prev_warn_codes

            degrade_decision = decide_degrade(
                anomalies=anomalies,
                now=cycle_now,
                current_override=current_override,
                cooldown_until=cooldown_until,
                last_reasons=last_reasons,
                recent_warn_count=warn_window_count,
                warn_threshold=settings.degrade_warn_threshold,
                warn_codes=warn_codes,
                recent_warn_codes=recent_warn_codes,
            )

            final_mode = combine_modes(risk_decision.mode, degrade_decision.mode_override)
            gated_actions = self._gate_actions_by_mode(accepted_actions, final_mode)
            prefiltered_actions, prefilter_min_notional_dropped = self._prefilter_submit_actions_min_notional(
                actions=gated_actions,
                pair_info=pair_info,
                min_order_notional_try=Decimal(str(settings.min_order_notional_try)),
                cycle_id=cycle_id,
            )
            if final_mode == Mode.OBSERVE_ONLY:
                logger.info(
                    "mode_gate_observe_only",
                    extra={"extra": {"cycle_id": cycle_id, "reasons": degrade_decision.reasons}},
                )

            execution_report = execution_service.execute_with_report(prefiltered_actions)
            self._assert_execution_invariant(execution_report)

            cycle_ended_at = datetime.now(UTC)
            updated_cycle_duration_ms = int((cycle_ended_at - cycle_started_at).total_seconds() * 1000)
            updated_anomalies = anomaly_detector.detect(
                market_data_age_seconds={
                    k: float(v) for k, v in market_snapshot.age_seconds_by_symbol.items()
                },
                reject_count=execution_report.rejected,
                cycle_duration_ms=updated_cycle_duration_ms,
                cursor_stall_by_symbol=cursor_stall_by_symbol,
                pnl_snapshot=snapshot,
                pnl_report=pnl_report,
            )
            updated_warn_codes = {
                event.code
                for event in updated_anomalies
                if event.severity == "WARN" and event.code in warn_codes
            }
            updated_has_warn = bool(updated_warn_codes)
            updated_warn_window_count = (prev_warn_window_count + 1) if updated_has_warn else 0
            updated_recent_warn_codes = updated_warn_codes or prev_warn_codes
            updated_decision = decide_degrade(
                anomalies=updated_anomalies,
                now=cycle_now,
                current_override=current_override,
                cooldown_until=cooldown_until,
                last_reasons=last_reasons,
                recent_warn_count=updated_warn_window_count,
                warn_threshold=settings.degrade_warn_threshold,
                warn_codes=warn_codes,
                recent_warn_codes=updated_recent_warn_codes,
            )

            anomaly_codes = [event.code.value for event in updated_anomalies]
            anomaly_severities = [event.severity for event in updated_anomalies]
            logger.info(
                "anomalies_detected",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "codes": anomaly_codes,
                        "severities": anomaly_severities,
                    }
                },
            )
            logger.info(
                "degrade_decision",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "override": (
                            updated_decision.mode_override.value
                            if updated_decision.mode_override
                            else None
                        ),
                        "cooldown_until": (
                            updated_decision.cooldown_until.isoformat()
                            if updated_decision.cooldown_until
                            else None
                        ),
                        "reasons": updated_decision.reasons,
                        "warn_window_count": updated_warn_window_count,
                    }
                },
            )
            final_mode = combine_modes(risk_decision.mode, updated_decision.mode_override)
            logger.info(
                "final_mode",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "base_mode": risk_decision.mode.value,
                        "override": (
                            updated_decision.mode_override.value
                            if updated_decision.mode_override
                            else None
                        ),
                        "final_mode": final_mode.value,
                    }
                },
            )

            try:
                state_store.persist_degrade(
                    cycle_id=cycle_id,
                    events=updated_anomalies,
                    cooldown_until=(
                        updated_decision.cooldown_until.isoformat()
                        if updated_decision.cooldown_until
                        else None
                    ),
                    current_override_mode=(
                        updated_decision.mode_override.value
                        if updated_decision.mode_override
                        else None
                    ),
                    last_reasons_json=json.dumps(updated_decision.reasons, sort_keys=True),
                    warn_window_count=updated_warn_window_count,
                    last_warn_codes_json=json.dumps(
                        sorted(code.value for code in updated_recent_warn_codes),
                        sort_keys=True,
                    ),
                    cursor_stall_cycles_json=json.dumps(cursor_stall_by_symbol, sort_keys=True),
                    last_reject_count=execution_report.rejected,
                )
            except Exception:  # noqa: BLE001
                try:
                    state_store.save_anomaly_events(cycle_id, updated_anomalies)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "anomaly_events_persist_failed", extra={"extra": {"cycle_id": cycle_id}}
                    )
            cycle_metrics: CycleMetrics = metrics_service.build_cycle_metrics(
                cycle_id=cycle_id,
                cycle_started_at=cycle_started_at,
                cycle_ended_at=datetime.now(UTC),
                mode=final_mode.value,
                fills=fills,
                fills_fetched_count=fills_fetched,
                fills_persisted_count=accounting_service.last_applied_fills_count,
                ledger_append_result=ledger_ingest,
                pnl_report=pnl_report,
                orders_submitted=execution_report.submitted,
                orders_canceled=execution_report.canceled,
                rejects_count=execution_report.rejected,
                mark_prices=mark_prices,
                pnl_snapshot=snapshot,
            )
            metrics_persisted = False
            try:
                with state_store.transaction():
                    metrics_service.persist_cycle_metrics(state_store, cycle_metrics)
                metrics_persisted = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cycle_metrics_persist_failed",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "reason_code": "cycle_metrics_persist_failed",
                            "error_type": type(exc).__name__,
                        }
                    },
                )

            decisions = lifecycle_plan.audit_reasons + [
                f"risk:{item.action.client_order_id or 'missing'}:{item.reason}"
                for item in risk_decisions
            ]
            decisions.extend(
                f"allocation:{item.status}:{item.reason}:{item.symbol}:{item.intent_index}"
                for item in decision_report.allocation_decisions
            )
            decisions.extend(
                f"action:{item.symbol}:{item.side}:{item.qty}:{item.notional_try}:"
                f"{item.strategy_id}:{item.intent_index}"
                for item in decision_report.allocation_actions
            )
            risk_decisions_from_audit = [
                entry for entry in decisions if isinstance(entry, str) and entry.startswith("risk:")
            ]
            accepted_by_risk = sum(
                1 for entry in risk_decisions_from_audit if entry.endswith(":accepted")
            )
            rejected_by_risk = sum(
                1
                for entry in risk_decisions_from_audit
                if entry.endswith(":rejected") or ":reject" in entry
            )

            # Counter semantics:
            # - pipeline_intents: intents emitted by DecisionPipelineService
            # - bootstrap_mapped_orders: fallback bootstrap orders mapped by Stage4 runner
            # - planned_actions: lifecycle actions that survived risk + prefilter gating
            # - rejects_total: all submit rejects from execution service
            # - rejected_min_notional: strict subset of rejects_total due to min-notional
            counts = {
                "ledger_events_attempted": ledger_ingest.events_attempted,
                "ledger_events_inserted": ledger_ingest.events_inserted,
                "ledger_events_ignored": ledger_ingest.events_ignored,
                "exchange_open": len(exchange_open_orders),
                "db_open": len(db_open_orders),
                "imported": len(reconcile_result.import_external),
                "enriched": len(reconcile_result.enrich_exchange_ids),
                "unknown_closed": len(reconcile_result.mark_unknown_closed),
                "external_missing_client_id": len(reconcile_result.external_missing_client_id),
                "fills_fetched": fills_fetched,
                "fills_applied": accounting_service.last_applied_fills_count,
                "cursor_before": sum(1 for value in cursor_before.values() if value is not None),
                "cursor_after": sum(1 for value in cursor_after.values() if value is not None),
                "planned_actions": len(prefiltered_actions),
                "pipeline_intents": len(decision_report.intents),
                "pipeline_order_requests": len(decision_report.order_requests),
                "pipeline_mapped_orders": decision_report.mapped_orders_count,
                "pipeline_dropped_actions": decision_report.dropped_actions_count,
                "bootstrap_mapped_orders": len(bootstrap_intents),
                "bootstrap_dropped_symbols": sum(bootstrap_drop_reasons.values()),
                "accepted_actions": len(accepted_actions),
                "prefilter_min_notional_dropped": prefilter_min_notional_dropped,
                "executed": execution_report.executed_total,
                "submitted": execution_report.submitted,
                "canceled": execution_report.canceled,
                "rejected_min_notional": execution_report.rejected_min_notional,
                "rejects_total": execution_report.rejected,
                "accepted_by_risk": accepted_by_risk,
                "rejected_by_risk": rejected_by_risk,
                "open_order_failures": open_order_failures,
                "fills_failures": fills_failures,
                "mark_price_failures": len(mark_price_errors),
            }
            counts.update(
                {f"alloc_{key}": value for key, value in dict(decision_report.counters).items()}
            )
            counts.update(
                {
                    f"pipeline_drop_{key}": value
                    for key, value in dict(decision_report.dropped_reasons).items()
                }
            )
            counts.update(
                {
                    f"bootstrap_drop_{key}": value
                    for key, value in dict(bootstrap_drop_reasons).items()
                }
            )
            state_store.record_cycle_audit(
                cycle_id=cycle_id, counts=counts, decisions=decisions, envelope=envelope
            )
            state_store.set_last_cycle_id(cycle_id)

            logger.info(
                "risk_decision",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "mode": risk_decision.mode.value,
                        "prev_mode": (prev_mode.value if prev_mode else None),
                        "reasons": risk_decision.reasons,
                        "drawdown_try": str(risk_decision.signals.drawdown_try),
                        "gross_exposure_try": str(risk_decision.signals.gross_exposure_try),
                        "fees_try_today": str(risk_decision.signals.fees_try_today),
                    }
                },
            )

            logger.info(
                "stage4_ledger_pnl_snapshot",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "realized_pnl_total": str(pnl_report.realized_pnl_total),
                        "unrealized_pnl_total": str(pnl_report.unrealized_pnl_total),
                        "fees_total_by_currency": {
                            k: str(v) for k, v in pnl_report.fees_total_by_currency.items()
                        },
                        "equity_estimate": str(pnl_report.equity_estimate),
                    }
                },
            )
            if metrics_persisted:
                logger.info(
                    "cycle_metrics",
                    extra={
                        "extra": {
                            "cycle_id": cycle_metrics.cycle_id,
                            "ts_start": cycle_metrics.ts_start.isoformat(),
                            "ts_end": cycle_metrics.ts_end.isoformat(),
                            "mode": cycle_metrics.mode,
                            "fills_count": cycle_metrics.fills_count,
                            "orders_submitted": cycle_metrics.orders_submitted,
                            "orders_canceled": cycle_metrics.orders_canceled,
                            "rejects_count": cycle_metrics.rejects_count,
                            "fills_per_submitted_order": cycle_metrics.fills_per_submitted_order,
                            "avg_time_to_fill": cycle_metrics.avg_time_to_fill,
                            "slippage_bps_avg": cycle_metrics.slippage_bps_avg,
                            "fees": cycle_metrics.fees,
                            "pnl": cycle_metrics.pnl,
                            "meta": cycle_metrics.meta,
                        }
                    },
                )
            reject_breakdown = {
                key: value
                for key, value in dict(decision_report.counters).items()
                if key.startswith("rejected") or key.startswith("scaled")
            }
            logger.info(
                "stage4_cycle_summary",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "mode": ("dry_run" if settings.dry_run else "live"),
                        "selected_universe": list(decision_report.selected_universe),
                        "planning_engine": planning_engine,
                        "kernel_plan_order_intents": (
                            len(kernel_plan.order_intents) if kernel_plan is not None else 0
                        ),
                        "equity_estimate_try": str(pnl_report.equity_estimate),
                        "intent_count": len(decision_report.intents),
                        "action_count": len(decision_report.allocation_actions),
                        "order_request_count": len(decision_report.order_requests),
                        "submitted": execution_report.submitted,
                        "canceled": execution_report.canceled,
                        "simulated": execution_report.simulated,
                        "rejects_breakdown": reject_breakdown,
                        "cursor_diagnostics": cursor_diag,
                        "cycle_duration_ms": updated_cycle_duration_ms,
                    }
                },
            )
            logger.info(
                "Stage 4 cycle completed", extra={"extra": {"cycle_id": cycle_id, **counts}}
            )
            return 0
        except ConfigurationError as exc:
            raise Stage4ConfigurationError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            if isinstance(
                exc, (Stage4ConfigurationError, Stage4ExchangeError, Stage4InvariantError)
            ):
                raise
            raise Stage4ExchangeError(str(exc)) from exc
        finally:
            self._close_best_effort(exchange, "exchange_stage4")

    def consume_shared_plan(self, plan: Plan, execution: ExecutionPort) -> list[str]:
        """Adapter glue for future migration to the shared PlanningKernel.

        TODO: invoke this from run_one_cycle once Stage4 planning duplication is removed.
        """

        return Stage4PlanConsumer(execution=execution).consume(plan)

    def _close_best_effort(self, resource: object, label: str) -> None:
        close = getattr(resource, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to close resource", extra={"extra": {"resource": label}}, exc_info=True
            )

    def _resolve_try_cash(self, exchange: object, *, fallback: Decimal) -> Decimal:
        base = getattr(exchange, "client", exchange)
        get_balances = getattr(base, "get_balances", None)
        if not callable(get_balances):
            return fallback
        try:
            balances = get_balances()
        except Exception:  # noqa: BLE001
            return fallback
        for balance in balances:
            if str(getattr(balance, "asset", "")).upper() == "TRY":
                return Decimal(str(getattr(balance, "free", 0)))
        return fallback

    def _safe_decimal(self, value: object) -> Decimal | None:
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not dec.is_finite() or dec < 0:
            return None
        return dec

    def resolve_mark_prices(
        self, exchange: object, symbols: list[str], *, cycle_now: datetime | None = None
    ) -> tuple[dict[str, Decimal], set[str]]:
        effective_now = cycle_now or datetime.now(UTC)
        snapshot = self._resolve_market_snapshot(exchange, symbols, cycle_now=effective_now)
        return snapshot.mark_prices, snapshot.anomalies

    def _resolve_mark_prices(
        self, exchange: object, symbols: list[str], *, cycle_now: datetime | None = None
    ) -> tuple[dict[str, Decimal], set[str]]:
        effective_now = cycle_now or datetime.now(UTC)
        snapshot = self._resolve_market_snapshot(exchange, symbols, cycle_now=effective_now)
        return snapshot.mark_prices, snapshot.anomalies

    def _resolve_market_snapshot(
        self, exchange: object, symbols: list[str], *, cycle_now: datetime
    ) -> MarketSnapshot:
        base = getattr(exchange, "client", exchange)
        get_orderbook = getattr(base, "get_orderbook", None)
        mark_prices: dict[str, Decimal] = {}
        orderbooks: dict[str, tuple[Decimal, Decimal]] = {}
        spreads_bps: dict[str, Decimal] = {}
        fetch_ages: list[Decimal] = []
        age_by_symbol: dict[str, Decimal] = {}
        fetched_at_by_symbol: dict[str, datetime | None] = {}
        anomalies: set[str] = set()
        if not callable(get_orderbook):
            return MarketSnapshot(
                mark_prices=mark_prices,
                orderbooks=orderbooks,
                anomalies=set(symbols),
                spreads_bps=spreads_bps,
                age_seconds_by_symbol={self.norm(symbol): Decimal("999999") for symbol in symbols},
                fetched_at_by_symbol={self.norm(symbol): None for symbol in symbols},
                max_data_age_seconds=Decimal("999999"),
            )

        get_orderbook_with_ts = getattr(base, "get_orderbook_with_timestamp", None)
        for symbol in symbols:
            normalized = self.norm(symbol)
            observed_at: datetime | None = None
            try:
                if callable(get_orderbook_with_ts):
                    bid_raw, ask_raw, observed_raw = get_orderbook_with_ts(symbol)
                    if isinstance(observed_raw, datetime):
                        observed_at = observed_raw.astimezone(UTC)
                else:
                    bid_raw, ask_raw = get_orderbook(symbol)
                    observed_at = datetime.now(UTC)
            except Exception:  # noqa: BLE001
                anomalies.add(normalized)
                fetch_ages.append(Decimal("999999"))
                age_by_symbol[normalized] = Decimal("999999")
                fetched_at_by_symbol[normalized] = None
                continue
            bid = self._safe_decimal(bid_raw)
            ask = self._safe_decimal(ask_raw)
            if bid is not None and ask is not None and ask < bid:
                bid, ask = ask, bid
                anomalies.add(normalized)
            if bid is not None and bid > 0 and ask is not None and ask > 0:
                orderbooks[normalized] = (bid, ask)
                mark = (bid + ask) / Decimal("2")
                spread_bps = (
                    ((ask - bid) / mark) * Decimal("10000") if mark > 0 else Decimal("999999")
                )
                spreads_bps[normalized] = max(Decimal("0"), spread_bps)
            elif bid is not None and bid > 0:
                orderbooks[normalized] = (bid, bid)
                mark = bid
                spreads_bps[normalized] = Decimal("999999")
            elif ask is not None and ask > 0:
                orderbooks[normalized] = (ask, ask)
                mark = ask
                spreads_bps[normalized] = Decimal("999999")
            else:
                anomalies.add(normalized)
                fetch_ages.append(Decimal("999999"))
                age_by_symbol[normalized] = Decimal("999999")
                continue
            mark_prices[normalized] = mark
            if observed_at is None:
                logger.warning(
                    "stale_market_data_timestamp_missing",
                    extra={"extra": {"symbol": normalized}},
                )
                anomalies.add(normalized)
                age_seconds = Decimal("999999")
            else:
                age_seconds = Decimal(str(max(0.0, (cycle_now - observed_at).total_seconds())))
            fetch_ages.append(age_seconds)
            age_by_symbol[normalized] = age_seconds
            fetched_at_by_symbol[normalized] = observed_at

        if not fetch_ages:
            max_age = Decimal("999999")
        else:
            max_age = max(fetch_ages)
        return MarketSnapshot(
            mark_prices=mark_prices,
            orderbooks=orderbooks,
            anomalies=anomalies,
            spreads_bps=spreads_bps,
            age_seconds_by_symbol=age_by_symbol,
            fetched_at_by_symbol=fetched_at_by_symbol,
            max_data_age_seconds=max_age,
        )

    def _apply_agent_policy(
        self,
        *,
        settings: Settings,
        state_store: StateStore,
        cycle_id: str,
        cycle_started_at: datetime,
        cycle_now: datetime,
        intents: list[Order],
        mark_prices: dict[str, Decimal],
        market_spreads_bps: dict[str, Decimal],
        market_data_age_seconds: Decimal,
        positions: list[Position],
        current_open_orders: list[Order],
        snapshot: object,
        live_mode: bool,
        failed_symbols: set[str],
        budget_guard_multiplier: Decimal,
    ) -> list[Order]:
        if not settings.agent_policy_enabled:
            return intents

        policy = self._resolve_agent_policy(settings)
        context = AgentContext(
            cycle_id=cycle_id,
            generated_at=cycle_now,
            market_snapshot=mark_prices,
            market_spreads_bps=market_spreads_bps,
            market_data_age_seconds=market_data_age_seconds,
            portfolio={
                self.norm(position.symbol): position.qty
                for position in positions
                if self.norm(position.symbol) not in failed_symbols
            },
            open_orders=[
                {
                    "symbol": self.norm(order.symbol),
                    "side": str(order.side),
                    "qty": str(order.qty),
                    "price": str(order.price),
                }
                for order in current_open_orders
            ],
            risk_state={
                "kill_switch": settings.kill_switch,
                "safe_mode": settings.safe_mode,
                "drawdown_pct": getattr(snapshot, "drawdown_pct", Decimal("0")),
                "gross_exposure_try": sum(
                    (
                        position.qty
                        * mark_prices.get(self.norm(position.symbol), position.avg_cost_try)
                    )
                    for position in positions
                ),
                "stale_data_seconds": Decimal(str(settings.stale_market_data_seconds)),
            },
            recent_events=[f"mark_price_error:{symbol}" for symbol in sorted(failed_symbols)],
            started_at=cycle_started_at,
            is_live_mode=live_mode,
        )

        base_decision = AgentDecision(
            action=DecisionAction.PROPOSE_INTENTS,
            propose_intents=[
                {
                    "symbol": self.norm(order.symbol),
                    "side": str(order.side),
                    "price_try": order.price,
                    "qty": order.qty,
                    "notional_try": order.price * order.qty,
                    "reason": order.client_order_id or "pipeline_intent",
                    "client_order_id": order.client_order_id,
                }
                for order in intents
            ],
            rationale=DecisionRationale(
                reasons=["Upstream planning intents"],
                confidence=1.0,
                constraints_hit=[],
                citations=["planning_kernel"],
            ),
        )

        evaluated = policy.evaluate(context)
        if evaluated.action in {DecisionAction.NO_OP, DecisionAction.ADJUST_RISK}:
            evaluated = base_decision

        guard_multiplier = min(
            Decimal("1"),
            max(Decimal("0"), Decimal(str(budget_guard_multiplier))),
        )

        guard = SafetyGuard(
            max_exposure_try=settings.risk_max_gross_exposure_try * guard_multiplier,
            max_order_notional_try=(
                (
                    settings.agent_max_order_notional_try
                    if settings.agent_max_order_notional_try > 0
                    else settings.risk_max_order_notional_try
                )
                * guard_multiplier
            ),
            max_drawdown_pct=settings.max_drawdown_pct,
            min_notional_try=Decimal(str(settings.min_order_notional_try)),
            max_spread_bps=settings.agent_max_spread_bps,
            symbol_allowlist={
                self.norm(symbol)
                for symbol in (settings.agent_symbol_allowlist or settings.symbols)
            },
            cooldown_seconds=settings.cooldown_seconds,
            stale_data_seconds=settings.stale_market_data_seconds,
            kill_switch=settings.kill_switch,
            safe_mode=settings.safe_mode,
            observe_only_override=settings.agent_observe_only,
        )
        safe_decision = guard.apply(context, evaluated)
        AgentAuditTrail(
            state_store=state_store,
            include_prompt_payloads=settings.agent_prompt_capture_enabled,
            max_payload_chars=settings.agent_prompt_capture_max_chars,
        ).persist(
            cycle_id=cycle_id,
            correlation_id=f"{cycle_id}:agent_policy",
            context=context,
            decision=evaluated,
            safe_decision=safe_decision,
        )

        return self._to_stage4_orders(
            safe_decision=safe_decision,
            now_utc=cycle_now,
            live_mode=live_mode,
        )

    def _resolve_agent_policy(self, settings: Settings) -> FallbackPolicy | RuleBasedPolicy:
        baseline = RuleBasedPolicy()
        if settings.agent_policy_provider.lower() != "llm":
            return baseline
        if not settings.agent_llm_enabled:
            logger.info(
                "llm_disabled_fallback",
                extra={"extra": {"provider": settings.agent_llm_provider}},
            )
            return baseline
        llm_policy = LlmPolicy(
            client=_UnavailableLlmClient(),
            prompt_builder=PromptBuilder(),
            timeout_seconds=settings.agent_llm_timeout_seconds,
        )
        return FallbackPolicy(primary=llm_policy, fallback=baseline)

    def _to_stage4_orders(
        self, *, safe_decision: object, now_utc: datetime, live_mode: bool
    ) -> list[Order]:
        if safe_decision.observe_only_override or safe_decision.decision.observe_only:
            return []
        mapped: list[Order] = []
        for item in safe_decision.decision.propose_intents:
            mapped.append(
                Order(
                    symbol=item.symbol,
                    side=item.side.lower(),
                    type="limit",
                    price=item.price_try,
                    qty=item.qty,
                    status="new",
                    created_at=now_utc,
                    updated_at=now_utc,
                    client_order_id=item.client_order_id,
                    mode="live" if live_mode else "dry_run",
                )
            )
        return mapped

    def _assert_execution_invariant(self, report: object) -> None:
        for field in ("executed_total", "submitted", "canceled", "simulated", "rejected", "rejected_min_notional"):
            if not hasattr(report, field):
                raise Stage4InvariantError(f"execution_report_missing_{field}")
            value = getattr(report, field)
            if not isinstance(value, int) or value < 0:
                raise Stage4InvariantError(f"execution_report_invalid_{field}")

    def _gate_actions_by_mode(
        self, actions: list[LifecycleAction], mode: Mode
    ) -> list[LifecycleAction]:
        if mode == Mode.OBSERVE_ONLY:
            return []
        if mode == Mode.REDUCE_RISK_ONLY:
            gated = []
            for action in actions:
                if action.action_type == LifecycleActionType.CANCEL:
                    gated.append(action)
                    continue
                if (
                    action.action_type == LifecycleActionType.SUBMIT
                    and str(action.side).upper() == "SELL"
                ):
                    gated.append(action)
            return gated
        return actions


    def _prefilter_submit_actions_min_notional(
        self,
        *,
        actions: list[LifecycleAction],
        pair_info: list[PairInfo] | None,
        min_order_notional_try: Decimal,
        cycle_id: str,
    ) -> tuple[list[LifecycleAction], int]:
        pair_info_by_symbol = {self.norm(item.pair_symbol): item for item in (pair_info or [])}
        filtered: list[LifecycleAction] = []
        dropped = 0
        for action in actions:
            if action.action_type != LifecycleActionType.SUBMIT:
                filtered.append(action)
                continue
            rules_source = pair_info_by_symbol.get(self.norm(action.symbol))
            if rules_source is None:
                filtered.append(action)
                continue
            rules = build_exchange_rules(rules_source)
            q_price = Quantizer.quantize_price(action.price, rules)
            q_qty = Quantizer.quantize_qty(action.qty, rules)
            notional_try = q_price * q_qty
            min_required = max(Decimal(str(min_order_notional_try)), rules.min_notional_try)
            if notional_try < min_required:
                dropped += 1
                logger.info(
                    "stage4_prefilter_drop_min_notional",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "symbol": self.norm(action.symbol),
                            "side": str(action.side),
                            "client_order_id": action.client_order_id,
                            "q_price": str(q_price),
                            "q_qty": str(q_qty),
                            "order_notional_try": str(notional_try),
                            "required_min_notional_try": str(min_required),
                            "reason_code": "prefilter_min_notional",
                        }
                    },
                )
                continue
            filtered.append(action)
        return filtered, dropped

    def _fills_cursor_key(self, symbol: str) -> str:
        return f"fills_cursor:{self.norm(symbol)}"

    def _to_position_summary(self, position: Position) -> PositionSummary:
        return PositionSummary(
            symbol=position.symbol,
            qty=position.qty,
            avg_cost=position.avg_cost_try,
        )

    def _resolve_pair_info(self, exchange: object) -> list[PairInfo] | None:
        base = getattr(exchange, "client", exchange)
        get_exchange_info = getattr(base, "get_exchange_info", None)
        if not callable(get_exchange_info):
            return None
        try:
            return list(get_exchange_info())
        except Exception:  # noqa: BLE001
            return None

    def _build_intents(
        self,
        *,
        cycle_id: str,
        min_order_notional_try: Decimal = Decimal("10"),
        bootstrap_notional_try: Decimal = Decimal("50"),
        max_notional_per_order_try: Decimal = Decimal("0"),
        symbols: list[str],
        mark_prices: dict[str, Decimal],
        try_cash: Decimal,
        open_orders: list[Order],
        live_mode: bool,
        bootstrap_enabled: bool,
        pair_info: list[PairInfo] | None,
        now_utc: datetime | None = None,
    ) -> tuple[list[Order], dict[str, int]]:
        if not bootstrap_enabled:
            return [], {}

        timestamp = now_utc or datetime.now(UTC)

        pair_info_by_symbol = {self.norm(item.pair_symbol): item for item in (pair_info or [])}
        intents: list[Order] = []
        drop_reasons: dict[str, int] = {}
        existing_keys = {(self.norm(order.symbol), order.side) for order in open_orders}
        for symbol in sorted(symbols):
            normalized = self.norm(symbol)
            rules_source = pair_info_by_symbol.get(normalized)
            if rules_source is None:
                self._inc_reason(drop_reasons, "missing_pair_info")
                continue

            mark = mark_prices.get(normalized)
            if mark is None or mark <= 0:
                self._inc_reason(drop_reasons, "missing_mark_price")
                continue
            if (normalized, "buy") in existing_keys:
                self._inc_reason(drop_reasons, "skipped_due_to_open_orders")
                logger.info(
                    "stage4_bootstrap_skipped_due_to_open_orders",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "symbol": normalized,
                            "reason_code": "skipped_due_to_open_orders",
                        }
                    },
                )
                continue

            rules = build_exchange_rules(rules_source)
            min_required_notional_try = max(min_order_notional_try, rules.min_notional_try)
            if try_cash < min_required_notional_try:
                self._inc_reason(drop_reasons, "cash_below_min_notional")
                logger.info(
                    "stage4_bootstrap_dropped_min_notional",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "symbol": normalized,
                            "budget_try": str(try_cash),
                            "min_required_notional_try": str(min_required_notional_try),
                            "reason_code": "cash_below_min_notional",
                        }
                    },
                )
                continue

            budget = min(try_cash, bootstrap_notional_try)
            if max_notional_per_order_try > 0:
                budget = min(budget, max_notional_per_order_try)
            if budget < min_required_notional_try:
                self._inc_reason(drop_reasons, "bootstrap_budget_below_min_notional")
                logger.info(
                    "stage4_bootstrap_dropped_min_notional",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "symbol": normalized,
                            "budget_try": str(budget),
                            "min_required_notional_try": str(min_required_notional_try),
                            "reason_code": "bootstrap_budget_below_min_notional",
                        }
                    },
                )
                continue

            qty_raw = budget / mark
            if qty_raw <= 0:
                continue

            price_q = Quantizer.quantize_price(mark, rules)
            qty_q = Quantizer.quantize_qty(qty_raw, rules)
            if qty_q <= 0:
                self._inc_reason(drop_reasons, "qty_became_zero")
                continue
            order_notional_try = price_q * qty_q
            if order_notional_try < min_required_notional_try:
                self._inc_reason(drop_reasons, "min_notional_after_quantize")
                continue

            intents.append(
                Order(
                    symbol=symbol,
                    side="buy",
                    type="limit",
                    price=price_q,
                    qty=qty_q,
                    status="new",
                    created_at=timestamp,
                    updated_at=timestamp,
                    client_order_id=f"s4-{cycle_id[:12]}-{normalized.lower()}-buy",
                    mode=("live" if live_mode else "dry_run"),
                )
            )
        return intents, drop_reasons

    @staticmethod
    def _translate_kernel_order_intents(
        *, order_intents: list[OrderIntent], now_utc: datetime, live_mode: bool
    ) -> list[Order]:
        translated: list[Order] = []
        for item in order_intents:
            if item.skipped:
                continue
            translated.append(
                Order(
                    symbol=item.symbol,
                    side=item.side.lower(),
                    type=item.order_type.lower(),
                    price=item.price_try,
                    qty=item.qty,
                    status="new",
                    created_at=now_utc,
                    updated_at=now_utc,
                    client_order_id=item.client_order_id,
                    mode=("live" if live_mode else "dry_run"),
                )
            )
        return translated

    @staticmethod
    def _inc_reason(reasons: dict[str, int], key: str) -> None:
        reasons[key] = reasons.get(key, 0) + 1

    @staticmethod
    def _parse_warn_codes(raw_codes: list[str]) -> set[AnomalyCode]:
        parsed: set[AnomalyCode] = set()
        for raw in raw_codes:
            try:
                parsed.add(AnomalyCode(str(raw)))
            except ValueError:
                continue
        return parsed

    @staticmethod
    def _safe_json_list(raw: str | None, *, default: list[str]) -> list[str]:
        if raw is None:
            return list(default)
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001
            return list(default)
        if not isinstance(parsed, list):
            return list(default)
        return [str(item) for item in parsed]

    @staticmethod
    def _safe_json_dict_int(raw: str | None, *, default: dict[str, int]) -> dict[str, int]:
        if raw is None:
            return dict(default)
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001
            return dict(default)
        if not isinstance(parsed, dict):
            return dict(default)
        result: dict[str, int] = {}
        for key, value in parsed.items():
            try:
                result[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return result
