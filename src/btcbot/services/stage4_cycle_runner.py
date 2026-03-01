from __future__ import annotations

# P0.2 diagnostics: surface compact reject reason/code/context in stage4_cycle_summary for fast triage.
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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
from btcbot.obs.alert_engine import AlertDedupe, AlertRuleEvaluator, LogNotifier, MetricWindowStore
from btcbot.obs.alerts import BASELINE_ALERT_RULES, DRY_RUN_ALERT_RULES
from btcbot.obs.metrics import observe_histogram, set_gauge
from btcbot.obs.process_role import ProcessRole, coerce_process_role
from btcbot.obs.stage4_alarm_hook import build_cycle_metrics
from btcbot.observability import get_instrumentation
from btcbot.observability_decisions import emit_decision
from btcbot.persistence.uow import UnitOfWorkFactory
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
from btcbot.services.market_data_service import MarketDataService
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

NO_ACTION_REASON_ENUM = {
    "NO_INTENTS_CREATED",
    "ALL_INTENTS_REJECTED_BY_RISK",
    "NO_EXECUTABLE_ACTIONS",
    "NO_SUBMISSIONS",
    "INSUFFICIENT_NOTIONAL_AFTER_BUFFERS",
    "INVALID_SYMBOL_FOR_INVENTORY_RESOLUTION",
    "INSUFFICIENT_INVENTORY_FREE_QTY",
    "INSUFFICIENT_SELL_QTY_INPUT",
    "QTY_BELOW_MIN_QTY_AFTER_QUANTIZE",
    "NOTIONAL_BELOW_MIN_TOTAL_AFTER_QUANTIZE",
    "NOTIONAL_BELOW_INTERNAL_MIN_AFTER_QUANTIZE",
    "SYMBOL_ON_COOLDOWN_1123",
}


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
    fetched_at_by_symbol: dict[str, datetime]
    max_data_age_seconds: Decimal
    dryrun_freshness_stale: bool = False
    dryrun_freshness_age_ms: int | None = None
    dryrun_freshness_missing_symbols_count: int = 0
    dryrun_ws_rest_fallback_used: bool = False


@dataclass(frozen=True)
class MarkPriceSafetyNetResult:
    success: bool
    symbol: str | None = None


class Stage4ConfigurationError(RuntimeError):
    pass


class Stage4ExchangeError(RuntimeError):
    pass


class Stage4InvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class Stage4CycleRunner:
    command: str = "stage4-run"
    _alert_store: MetricWindowStore = dataclass_field(init=False, repr=False)
    _alert_evaluator: AlertRuleEvaluator = dataclass_field(init=False, repr=False)
    _alert_dedupe: AlertDedupe = dataclass_field(init=False, repr=False)
    _alert_notifier: LogNotifier = dataclass_field(init=False, repr=False)
    _dryrun_consecutive_exchange_degraded: int = dataclass_field(init=False, repr=False, default=0)
    _last_cycle_completed_epoch: int | None = dataclass_field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_alert_store", MetricWindowStore())
        object.__setattr__(self, "_alert_evaluator", AlertRuleEvaluator())
        object.__setattr__(self, "_alert_dedupe", AlertDedupe())
        object.__setattr__(self, "_alert_notifier", LogNotifier(logger))
        object.__setattr__(self, "_dryrun_consecutive_exchange_degraded", 0)
        object.__setattr__(self, "_last_cycle_completed_epoch", None)

    @staticmethod
    def norm(symbol: str) -> str:
        return normalize_symbol(symbol)

    def run_one_cycle(self, settings: Settings, *, force_dry_run_submit: bool = False) -> int:
        instrumentation = get_instrumentation()
        cycle_started_monotonic = datetime.now(UTC)
        if settings is not None and settings.dry_run:
            instrumentation.counter("dryrun_cycle_started_total", 1)
            last_completed_epoch = self._last_cycle_completed_epoch
            if last_completed_epoch is not None:
                stall_seconds = max(
                    0, int(datetime.now(UTC).timestamp()) - int(last_completed_epoch)
                )
                self._alert_store.record(
                    "dryrun_cycle_stall_seconds",
                    stall_seconds,
                    int(datetime.now(UTC).timestamp()),
                )
        exchange = build_exchange_stage4(settings, dry_run=settings.dry_run)
        live_mode = settings.is_live_trading_enabled() and not settings.dry_run
        state_store = StateStore(db_path=settings.state_db_path)
        uow_factory = UnitOfWorkFactory(settings.state_db_path)
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
        dynamic_universe_fallback_triggered = False
        dynamic_universe_fallback_reason = "not_needed"
        pre_cycle_degrade_state = state_store.get_degrade_state_current()
        pre_cycle_reasons = self._safe_json_dict(pre_cycle_degrade_state.get("last_reasons_json"), default={})
        pre_cycle_level = int(pre_cycle_reasons.get("level", 0) or 0)
        pre_cycle_universe_cap_raw = pre_cycle_reasons.get("universe_cap")
        pre_cycle_universe_cap = None
        if pre_cycle_universe_cap_raw is not None:
            try:
                pre_cycle_universe_cap = max(1, int(pre_cycle_universe_cap_raw))
            except (TypeError, ValueError):
                pre_cycle_universe_cap = None
        if pre_cycle_level >= 1 and pre_cycle_universe_cap is not None:
            active_symbols = active_symbols[:pre_cycle_universe_cap]
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
            elif settings.dry_run:
                active_symbols = [self.norm(symbol) for symbol in settings.symbols]
                dynamic_universe_fallback_triggered = True
                dynamic_universe_fallback_reason = "dry_run_empty_selection"
            elif live_mode and settings.dynamic_universe_live_fallback_enabled:
                # Keep LIVE cycles productive when dynamic selection returns empty; disable with
                # DYNAMIC_UNIVERSE_LIVE_FALLBACK_ENABLED=false if strict empty-universe behavior is desired.
                active_symbols = [self.norm(symbol) for symbol in settings.symbols]
                dynamic_universe_fallback_triggered = True
                dynamic_universe_fallback_reason = "live_empty_selection"
            elif live_mode:
                dynamic_universe_fallback_reason = "live_empty_selection_fallback_disabled"
                instrumentation.counter(
                    "stage4_dynamic_universe_empty_live_no_fallback_total",
                    1,
                )
                logger.warning(
                    "stage4_dynamic_universe_empty_live_no_fallback",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "dynamic_universe_live_fallback_enabled": False,
                            "configured_symbols": [self.norm(symbol) for symbol in settings.symbols],
                        }
                    },
                )

            if dynamic_universe_fallback_triggered or dynamic_universe_fallback_reason != "not_needed":
                instrumentation.counter(
                    "stage4_dynamic_universe_fallback_total",
                    1,
                    attrs={"reason": dynamic_universe_fallback_reason},
                )
            instrumentation.gauge("stage4_active_symbols_count", float(len(active_symbols)))

            logger.info(
                "stage4_dynamic_universe_resolution",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "selection_count": len(selection.selected_symbols),
                        "fallback_triggered": dynamic_universe_fallback_triggered,
                        "fallback_reason": dynamic_universe_fallback_reason,
                        "active_symbols": sorted(self.norm(symbol) for symbol in active_symbols),
                    }
                },
            )

        process_role = coerce_process_role(getattr(settings, "process_role", None)).value
        effective_kill_switch, db_kill_switch, kill_switch_source = self._resolve_effective_kill_switch(
            settings=settings, state_store=state_store, process_role=process_role
        )
        metadata_optional_mode = (
            settings.dry_run or settings.safe_mode or process_role == ProcessRole.MONITOR.value
        )
        if not pair_info:
            if live_mode and not metadata_optional_mode:
                raise Stage4ExchangeError("missing_exchange_metadata")
            logger.warning(
                "stage4_exchange_metadata_missing",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "live_mode": live_mode,
                        "metadata_optional_mode": metadata_optional_mode,
                        "reason_code": "missing_exchange_metadata",
                    }
                },
            )

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
                max_order_notional_try=Decimal(str(settings.risk_max_order_notional_try)),
                max_position_notional_try=settings.max_position_notional_try,
                max_daily_loss_try=settings.max_daily_loss_try,
                max_drawdown_pct=settings.max_drawdown_pct,
                fee_bps_taker=settings.fee_bps_taker,
                slippage_bps_buffer=settings.slippage_bps_buffer,
                min_profit_bps=Decimal(str(settings.min_profit_bps)),
                replace_inflight_budget_per_symbol_try=Decimal(
                    str(settings.replace_inflight_budget_per_symbol_try)
                ),
                max_gross_exposure_try=Decimal(str(settings.risk_max_gross_exposure_try)),
            )
            try:
                risk_budget_service = RiskBudgetService(
                    state_store=state_store, uow_factory=uow_factory
                )
            except TypeError:
                risk_budget_service = RiskBudgetService(state_store=state_store)
            settings.kill_switch_effective = effective_kill_switch
            settings.kill_switch_source = kill_switch_source
            execution_service = ExecutionService(
                exchange=exchange,
                state_store=state_store,
                settings=settings,
                rules_service=rules_service,
            )
            logger.info(
                "stage4_killswitch_state",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "process_role": process_role,
                        "settings_kill_switch": bool(settings.kill_switch),
                        "db_kill_switch": bool(db_kill_switch),
                        "effective_kill_switch": bool(effective_kill_switch),
                        "source": kill_switch_source,
                        "freeze_all": bool(settings.kill_switch_freeze_all),
                    }
                },
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
                settings=settings,
            )
            mark_prices = market_snapshot.mark_prices
            stale_symbols = {
                symbol
                for symbol, age in market_snapshot.age_seconds_by_symbol.items()
                if age > Decimal(str(settings.stale_market_data_seconds))
            }
            if settings.dry_run and (
                type(getattr(exchange, "client", exchange)).__name__ == "DryRunExchangeClient"
            ):
                stale_symbols = set()
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

            db_open_orders = state_store.list_stage4_open_orders(include_unknown=True)
            try:
                reconcile_result = reconcile_service.resolve(
                    exchange_open_orders=exchange_open_orders,
                    db_open_orders=db_open_orders,
                    failed_symbols=failed_symbols,
                )
            except TypeError:
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

            for symbol in active_symbols:
                normalized = self.norm(symbol)
                blocked = normalized in failed_symbols
                instrumentation.counter(
                    "stage4_reconcile_unknown_closed_total",
                    sum(
                        1
                        for client_order_id in reconcile_result.mark_unknown_closed
                        for order in db_open_orders
                        if order.client_order_id == client_order_id
                        and self.norm(order.symbol) == normalized
                    ),
                    attrs={"symbol": normalized},
                )
                instrumentation.counter(
                    "stage4_reconcile_external_import_total",
                    sum(
                        1
                        for order in reconcile_result.import_external
                        if self.norm(order.symbol) == normalized
                    ),
                    attrs={"symbol": normalized},
                )
                logger.info(
                    "stage4_reconcile_summary",
                    extra={
                        "extra": {
                            "symbol": normalized,
                            "mark_unknown_closed": sum(
                                1
                                for client_order_id in reconcile_result.mark_unknown_closed
                                for order in db_open_orders
                                if order.client_order_id == client_order_id
                                and self.norm(order.symbol) == normalized
                            ),
                            "import_external": sum(
                                1
                                for order in reconcile_result.import_external
                                if self.norm(order.symbol) == normalized
                            ),
                            "enrich_exchange_ids": sum(
                                1
                                for client_order_id, _ in reconcile_result.enrich_exchange_ids
                                for order in db_open_orders
                                if order.client_order_id == client_order_id
                                and self.norm(order.symbol) == normalized
                            ),
                            "external_missing_client_id": sum(
                                1
                                for order in reconcile_result.external_missing_client_id
                                if self.norm(order.symbol) == normalized
                            ),
                            "blocked": blocked,
                        }
                    },
                )

            freeze_state, freeze_triggered = self._evaluate_unknown_order_freeze(
                settings=settings,
                state_store=state_store,
                process_role=process_role,
                cycle_now=cycle_now,
                exchange_open_orders=exchange_open_orders,
                reconcile_result=reconcile_result,
                db_open_orders=db_open_orders,
                instrumentation=instrumentation,
            )
            instrumentation.gauge(
                "stage4_freeze_active",
                1 if freeze_state.active else 0,
                attrs={"process_role": process_role},
            )

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
                    cursor_diag[normalized]["fills_seen"] = fetched.fills_seen
                    cursor_diag[normalized]["prefilter_deduped"] = fetched.fills_deduped
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
                fills_seen = int(diag.get("fills_seen", 0) or 0)
                prefilter_deduped = int(diag.get("prefilter_deduped", 0) or 0)
                apply_stats = accounting_service.last_apply_stats_by_symbol.get(symbol, {})
                state_deduped = int(apply_stats.get("deduped", 0) or 0)
                new_count = int(apply_stats.get("new", 0) or 0)
                deduped_count = prefilter_deduped + state_deduped
                diag["deduped_count"] = deduped_count
                diag["persisted_count"] = new_count

                instrumentation.counter(
                    "stage4_fills_seen_total",
                    fills_seen,
                    attrs={"symbol": symbol},
                )
                instrumentation.counter(
                    "stage4_fills_new_total",
                    new_count,
                    attrs={"symbol": symbol},
                )
                instrumentation.counter(
                    "stage4_fills_deduped_total",
                    deduped_count,
                    attrs={"symbol": symbol},
                )
                logger.info(
                    "stage4_fill_ingest",
                    extra={
                        "extra": {
                            "symbol": symbol,
                            "fills_seen": fills_seen,
                            "new": new_count,
                            "deduped": deduped_count,
                            "cursor_before": diag.get("cursor_before"),
                            "cursor_after": diag.get("cursor_after"),
                        }
                    },
                )
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
            for missing_currency in pnl_report.fee_conversion_missing_currencies:
                instrumentation.counter(
                    "stage4_fee_conversion_missing_total",
                    1,
                    attrs={"currency": missing_currency},
                )
            ledger_checkpoint = ledger_service.checkpoint()
            capital_result = None
            try:
                capital_result = risk_budget_service.apply_self_financing_checkpoint(
                    cycle_id=cycle_id,
                    realized_pnl_total_try=pnl_report.realized_pnl_total,
                    ledger_event_count=ledger_checkpoint.event_count_total_applied,
                    ledger_checkpoint_id=ledger_checkpoint.checkpoint_id,
                    # NOTE: seed from available TRY cash for conservative capital bootstrap.
                    # Switching to equity_estimate would include MTM and is intentionally deferred.
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
                            "ledger_event_count": ledger_checkpoint.event_count_total_applied,
                            "error": str(exc),
                        },
                    },
                )
                raise Stage4InvariantError(str(exc)) from exc

            instrumentation.gauge("stage4.ledger.net_pnl_try", float(pnl_report.realized_pnl_total + pnl_report.unrealized_pnl_total - (pnl_report.fees_total_try or Decimal("0"))))
            instrumentation.gauge("stage4.ledger.fees_try", float(pnl_report.fees_total_try or Decimal("0")))
            instrumentation.gauge("stage4.ledger.equity_try", float(pnl_report.equity_estimate))
            instrumentation.counter(
                "stage4.ledger.missing_currencies",
                len(pnl_report.fee_conversion_missing_currencies),
            )
            if capital_result is not None:
                instrumentation.gauge(
                    "capital_policy.trading_capital_try",
                    float(capital_result.trading_capital_try),
                )
                instrumentation.gauge(
                    "capital_policy.treasury_try",
                    float(capital_result.treasury_try),
                )
                instrumentation.counter(
                    "capital_policy.checkpoint_applied",
                    1,
                    attrs={"applied": "true" if capital_result.applied else "false"},
                )
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
            eligible_symbols = [
                symbol for symbol in active_symbols if self.norm(symbol) not in failed_symbols
            ]
            tradable_symbols_before_coverage = list(eligible_symbols)
            missing_mark_symbols = [
                symbol
                for symbol in eligible_symbols
                if (
                    mark_prices.get(self.norm(symbol)) is None
                    or mark_prices.get(self.norm(symbol), Decimal("0")) <= 0
                )
            ]
            covered_symbols = [
                symbol
                for symbol in eligible_symbols
                if mark_prices.get(self.norm(symbol), Decimal("0")) > 0
            ]

            performed_safety_net = False
            safety_net_symbol: str | None = None
            safety_net_success = False
            if live_mode and not covered_symbols:
                performed_safety_net = True
                coverage_result = self._try_recover_single_mark_price(
                    exchange=exchange,
                    active_symbols=active_symbols,
                    mark_prices=mark_prices,
                )
                safety_net_symbol = coverage_result.symbol
                safety_net_success = coverage_result.success
                if coverage_result.success and coverage_result.symbol is not None:
                    covered_symbols = [coverage_result.symbol]
                    missing_mark_symbols = [
                        symbol for symbol in missing_mark_symbols if symbol != coverage_result.symbol
                    ]

            coverage_ratio = self._compute_mark_price_coverage_ratio(
                covered_symbols=covered_symbols,
                tradeable_symbols_requested=tradable_symbols_before_coverage,
            )
            instrumentation.counter(
                "stage4_mark_price_missing_symbols_total", len(missing_mark_symbols)
            )
            instrumentation.gauge("stage4_mark_price_coverage_ratio", coverage_ratio)
            if coverage_ratio < float(settings.mark_price_min_coverage_ratio):
                instrumentation.counter("stage4_mark_price_coverage_below_min_total", 1)
            if performed_safety_net:
                instrumentation.counter("stage4_mark_price_safety_net_attempt_total", 1)
            if safety_net_success:
                instrumentation.counter("stage4_mark_price_safety_net_success_total", 1)

            log_mark_price_coverage = logger.warning if (
                len(missing_mark_symbols) >= settings.mark_price_missing_symbols_warn_threshold
                or coverage_ratio < float(settings.mark_price_min_coverage_ratio)
            ) else logger.info
            log_mark_price_coverage(
                "stage4_mark_price_coverage",
                extra={
                    "extra": {
                        "event": "stage4_mark_price_coverage",
                        "cycle_id": cycle_id,
                        "active_symbols_count": len(active_symbols),
                        "failed_symbols_count": len(failed_symbols),
                        "mark_prices_count": len(mark_prices),
                        "tradable_symbols_count_before": len(tradable_symbols_before_coverage),
                        "tradable_symbols_count_after": len(covered_symbols),
                        "coverage_ratio": coverage_ratio,
                        "coverage_ratio_min": float(settings.mark_price_min_coverage_ratio),
                        "missing_mark_symbols_count": len(missing_mark_symbols),
                        "missing_mark_symbols_sample": sorted(missing_mark_symbols)[:10],
                        "performed_safety_net": performed_safety_net,
                        "safety_net_symbol": safety_net_symbol,
                        "safety_net_success": safety_net_success,
                    }
                },
            )

            budget_decision, prev_mode, peak_equity, fees_today, risk_day = (
                risk_budget_service.compute_decision(
                    cycle_id=cycle_id,
                    limits=risk_limits,
                    pnl_report=pnl_report,
                    positions=positions,
                    mark_prices=mark_prices,
                    realized_today_try=snapshot.realized_today_try,
                    kill_switch_active=effective_kill_switch,
                    live_mode=live_mode,
                    tradable_symbols=covered_symbols,
                )
            )
            budget_notional_multiplier = getattr(
                budget_decision, "position_sizing_multiplier", Decimal("1")
            )
            if effective_kill_switch:
                budget_notional_multiplier = Decimal("0")

            if settings.stage4_use_planning_kernel:
                kernel_result = build_stage4_kernel_plan(
                    settings=settings,
                    cycle_id=cycle_id,
                    now_utc=cycle_now,
                    selected_symbols=covered_symbols,
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
                    preferred_symbols=covered_symbols,
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
            pipeline_orders: list[Order] = []
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

            if (
                settings.dry_run
                and not intents
                and not pipeline_orders
                and not bootstrap_intents
                and (
                    not pair_info
                    or bootstrap_drop_reasons.get("missing_pair_info", 0) > 0
                    or force_dry_run_submit
                )
            ):
                metadata_free_bootstrap = self._build_metadata_free_dry_run_bootstrap_intents(
                    cycle_id=cycle_id,
                    min_order_notional_try=Decimal(str(settings.min_order_notional_try)),
                    bootstrap_notional_try=Decimal(str(settings.stage5_bootstrap_notional_try)),
                    symbols=[
                        symbol
                        for symbol in active_symbols
                        if self.norm(symbol) not in failed_symbols
                    ],
                    mark_prices=mark_prices,
                    try_cash=try_cash,
                    open_orders=current_open_orders,
                    live_mode=live_mode,
                    now_utc=cycle_now,
                )
                if metadata_free_bootstrap:
                    bootstrap_intents = metadata_free_bootstrap
                    intents = metadata_free_bootstrap

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
            open_orders_by_client_id = {
                order.client_order_id: order
                for order in current_open_orders
                if order.client_order_id
            }
            accepted_actions, risk_decisions = risk_policy.filter_actions(
                safe_actions,
                open_orders_count=len(current_open_orders),
                current_position_notional_try=current_position_notional,
                pnl=snapshot,
                positions_by_symbol=positions_by_symbol,
                open_orders_by_client_id=open_orders_by_client_id,
            )
            for decision in risk_decisions:
                if decision.accepted:
                    continue
                instrumentation.counter(
                    "stage4_action_filtered_total",
                    1,
                    attrs={
                        "reason": decision.reason,
                        "process_role": process_role,
                        "status": "rejected",
                    },
                )

            risk_decision = getattr(budget_decision, "risk_decision", budget_decision)
            instrumentation.gauge("risk_budget.multiplier", float(budget_decision.position_sizing_multiplier))
            instrumentation.counter(
                "risk_budget.reasons_count",
                len(getattr(risk_decision, "reasons", [])),
                attrs={"mode": risk_decision.mode.value},
            )
            instrumentation.counter(
                "risk_budget.mode",
                1,
                attrs={"mode": risk_decision.mode.value},
            )
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
            last_reasons_payload = self._safe_json_dict(
                degrade_state.get("last_reasons_json"), default={}
            )
            last_reasons = [str(item) for item in last_reasons_payload.get("reasons", [])]
            previous_level = int(last_reasons_payload.get("level", 0) or 0)
            previous_recovery_streak = int(last_reasons_payload.get("recovery_streak", 0) or 0)
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
                    if (
                        before_value is not None
                        and before_value == after_value
                        and not dedupe_only_cycle
                    ):
                        cursor_stall_by_symbol[symbol] = prev + 1
                    else:
                        cursor_stall_by_symbol[symbol] = 0

            cycle_observed_at = datetime.now(UTC)
            cycle_duration_ms = int((cycle_observed_at - cycle_started_at).total_seconds() * 1000)
            cycle_duration_seconds = float(cycle_duration_ms) / 1000.0
            instrumentation.gauge("stage4_cycle_duration_seconds", cycle_duration_seconds)
            max_cursor_stall_cycles = max(cursor_stall_by_symbol.values(), default=0)
            now_epoch = int(cycle_observed_at.timestamp())
            if max_cursor_stall_cycles >= int(settings.cursor_stall_cycles):
                instrumentation.counter(
                    "stage4_stuck_cycles_total",
                    1,
                    attrs={"reason": "cursor_stall"},
                )
                self._alert_store.record("stage4_stuck_cycle_cursor_stall_total", 1, now_epoch)
            if cycle_duration_seconds >= float(settings.stuck_cycle_seconds):
                instrumentation.counter(
                    "stage4_stuck_cycles_total",
                    1,
                    attrs={"reason": "duration"},
                )
                self._alert_store.record("stage4_stuck_cycle_duration_total", 1, now_epoch)
            logger.info(
                "stage4_cycle_health",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "duration_seconds": cycle_duration_seconds,
                        "cursor_before_count": sum(1 for value in cursor_before.values() if value is not None),
                        "cursor_after_count": sum(1 for value in cursor_after.values() if value is not None),
                        "cursor_stall_by_symbol_count": sum(1 for value in cursor_stall_by_symbol.values() if value > 0),
                        "max_cursor_stall_cycles": max_cursor_stall_cycles,
                    }
                },
            )
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

            health_snapshot_fn = getattr(exchange, "health_snapshot", None)
            api_snapshot = health_snapshot_fn() if callable(health_snapshot_fn) else {}
            breaker_is_open = bool((api_snapshot or {}).get("breaker_open", False))
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
                previous_level=previous_level,
                breaker_open=breaker_is_open,
                freeze_active=False,
                stability_streak=previous_recovery_streak,
            )

            final_mode = combine_modes(risk_decision.mode, degrade_decision.mode_override)
            if force_dry_run_submit:
                final_mode = Mode.NORMAL
            if degrade_decision.universe_cap is not None:
                allowed_symbols = set(active_symbols[: degrade_decision.universe_cap])
                accepted_actions = [
                    action for action in accepted_actions if self.norm(action.symbol) in allowed_symbols
                ]
            if degrade_decision.shrink_notional_factor < 1:
                shrinked_actions: list[LifecycleAction] = []
                shrink_factor = Decimal(str(degrade_decision.shrink_notional_factor))
                for action in accepted_actions:
                    if (
                        action.action_type == LifecycleActionType.SUBMIT
                        and str(action.side).upper() == "BUY"
                    ):
                        shrinked_actions.append(
                            LifecycleAction(
                                action_type=action.action_type,
                                symbol=action.symbol,
                                side=action.side,
                                price=action.price,
                                qty=action.qty * shrink_factor,
                                reason=action.reason,
                                client_order_id=action.client_order_id,
                                exchange_order_id=action.exchange_order_id,
                                replace_for_client_order_id=action.replace_for_client_order_id,
                            )
                        )
                        continue
                    shrinked_actions.append(action)
                accepted_actions = shrinked_actions
            gated_actions = self._gate_actions_by_mode(accepted_actions, final_mode)
            prefiltered_actions, prefilter_min_notional_dropped = (
                self._prefilter_submit_actions_min_notional(
                    actions=gated_actions,
                    pair_info=pair_info,
                    min_order_notional_try=Decimal(str(settings.min_order_notional_try)),
                    cycle_id=cycle_id,
                )
            )
            if final_mode == Mode.OBSERVE_ONLY:
                logger.info(
                    "mode_gate_observe_only",
                    extra={"extra": {"cycle_id": cycle_id, "reasons": degrade_decision.reasons}},
                )

            prefiltered_actions, killswitch_suppressed_counts = self._suppress_actions_for_killswitch(
                actions=prefiltered_actions,
                effective_kill_switch=effective_kill_switch,
                freeze_all=bool(settings.kill_switch_freeze_all),
                process_role=process_role,
                kill_switch_source=kill_switch_source,
                instrumentation=instrumentation,
            )
            prefiltered_actions, freeze_suppressed_counts = self._suppress_actions_for_unknown_freeze(
                actions=prefiltered_actions,
                freeze_active=bool(freeze_state.active),
                freeze_all=bool(settings.kill_switch_freeze_all),
                process_role=process_role,
                instrumentation=instrumentation,
            )
            if settings.dry_run and force_dry_run_submit:
                planned_submit_actions = [
                    action
                    for action in prefiltered_actions
                    if action.action_type == LifecycleActionType.SUBMIT
                ]
                persist_intents = intents
                if not planned_submit_actions and not persist_intents:
                    fallback_symbols = list(active_symbols) or [self.norm(symbol) for symbol in settings.symbols]
                    persist_intents = self._build_metadata_free_dry_run_bootstrap_intents(
                        cycle_id=cycle_id,
                        min_order_notional_try=Decimal(str(settings.min_order_notional_try)),
                        bootstrap_notional_try=Decimal(str(settings.stage5_bootstrap_notional_try)),
                        symbols=fallback_symbols,
                        mark_prices=mark_prices,
                        try_cash=try_cash,
                        open_orders=current_open_orders,
                        live_mode=live_mode,
                        now_utc=cycle_now,
                    )
                    if not persist_intents and fallback_symbols:
                        fallback_symbol = self.norm(fallback_symbols[0])
                        fallback_price = mark_prices.get(fallback_symbol, Decimal("1"))
                        if fallback_price > 0:
                            min_notional = max(Decimal(str(settings.min_order_notional_try)), Decimal("1"))
                            fallback_qty = min_notional / fallback_price
                            persist_intents = [
                                Order(
                                    symbol=fallback_symbol,
                                    side="buy",
                                    type="limit",
                                    price=fallback_price,
                                    qty=fallback_qty,
                                    status="new",
                                    created_at=cycle_now,
                                    updated_at=cycle_now,
                                    client_order_id=f"s4-{cycle_id[:12]}-{fallback_symbol.lower()}-buy",
                                    mode="dry_run",
                                )
                            ]
                self._persist_dry_run_planned_order(
                    state_store=state_store,
                    cycle_id=cycle_id,
                    planned_actions=planned_submit_actions,
                    intents=persist_intents,
                )
            execution_report = execution_service.execute_with_report(prefiltered_actions)
            self._assert_execution_invariant(execution_report)

            cycle_ended_at = datetime.now(UTC)
            updated_cycle_duration_ms = int(
                (cycle_ended_at - cycle_started_at).total_seconds() * 1000
            )
            observe_histogram(
                "bot_cycle_latency_ms",
                updated_cycle_duration_ms,
                labels={"process_role": process_role, "mode_final": final_mode.value},
            )
            set_gauge(
                "bot_killswitch_enabled",
                1 if bool(effective_kill_switch) else 0,
                labels={"process_role": process_role},
            )
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
                previous_level=degrade_decision.level,
                breaker_open=breaker_is_open,
                freeze_active=False,
                stability_streak=degrade_decision.recovery_streak,
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
                        "applied_override": (
                            degrade_decision.mode_override.value
                            if degrade_decision.mode_override
                            else None
                        ),
                        "applied_cooldown_until": (
                            degrade_decision.cooldown_until.isoformat()
                            if degrade_decision.cooldown_until
                            else None
                        ),
                        "applied_reasons": degrade_decision.reasons,
                        "next_override": (
                            updated_decision.mode_override.value
                            if updated_decision.mode_override
                            else None
                        ),
                        "next_level": updated_decision.level,
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
                        degrade_decision.cooldown_until.isoformat()
                        if degrade_decision.cooldown_until
                        else None
                    ),
                    current_override_mode=(
                        degrade_decision.mode_override.value
                        if degrade_decision.mode_override
                        else None
                    ),
                    last_reasons_json=json.dumps(
                        {
                            "level": degrade_decision.level,
                            "override_mode": (
                                degrade_decision.mode_override.value
                                if degrade_decision.mode_override
                                else None
                            ),
                            "reasons": degrade_decision.reasons,
                            "shrink_notional_factor": degrade_decision.shrink_notional_factor,
                            "universe_cap": degrade_decision.universe_cap,
                            "recovery_streak": degrade_decision.recovery_streak,
                            "next_level": updated_decision.level,
                            "next_override_mode": (
                                updated_decision.mode_override.value
                                if updated_decision.mode_override
                                else None
                            ),
                        },
                        sort_keys=True,
                    ),
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
            instrumentation.gauge(
                "stage4.ledger.slippage_try",
                float(getattr(snapshot, "slippage_try", Decimal("0"))),
            )
            instrumentation.gauge(
                "stage4.ledger.turnover_try",
                float(getattr(snapshot, "turnover_try", Decimal("0"))),
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
                f"risk:{item.action.client_order_id or 'missing'}:{'accepted' if item.accepted else 'rejected'}:{item.reason}"
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
            accepted_by_risk = 0
            rejected_by_risk = 0
            for entry in risk_decisions_from_audit:
                parts = entry.split(":", 3)
                if len(parts) != 4:
                    continue
                status = parts[2]
                if status == "accepted":
                    accepted_by_risk += 1
                elif status == "rejected":
                    rejected_by_risk += 1

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
                "freeze_active": int(freeze_state.active),
                "freeze_triggered": int(freeze_triggered),
                "freeze_suppressed_submit": (
                    freeze_suppressed_counts["submit"] + freeze_suppressed_counts["replace_submit"]
                ),
                "freeze_suppressed_cancel": freeze_suppressed_counts["cancel"],
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
            api_snapshot: dict[str, object] = {}
            rejects_by_code: dict[str, int] = {}
            try:
                health_snapshot_fn = getattr(exchange, "health_snapshot", None)
                api_snapshot = health_snapshot_fn() if callable(health_snapshot_fn) else {}
                breaker_is_open = bool((api_snapshot or {}).get("breaker_open", False))
                degraded_mode = bool((api_snapshot or {}).get("degraded", False) or breaker_is_open)
                rejects_by_code = self._extract_rejects_by_code(decision_report.counters)
                for code, count in dict(getattr(execution_report, "rejected_by_code", {})).items():
                    rejects_by_code[str(code)] = rejects_by_code.get(str(code), 0) + int(count)
                rejects_by_code = dict(sorted(rejects_by_code.items()))
                state_store.save_stage4_run_metrics(
                    cycle_id=cycle_id,
                    ts=cycle_ended_at,
                    reasons_no_action=self._reasons_no_action_enum(
                        intents_created=len(intents),
                        intents_after_risk=len(accepted_actions),
                        intents_executed=execution_report.executed_total,
                        orders_submitted=execution_report.submitted,
                        rejects_by_code=rejects_by_code,
                        intent_skip_reasons=[
                            getattr(item, "skip_reason", None)
                            for item in intents
                            if bool(getattr(item, "skipped", False))
                        ],
                    ),
                    intents_created=len(intents),
                    intents_after_risk=len(accepted_actions),
                    intents_executed=execution_report.executed_total,
                    orders_submitted=execution_report.submitted,
                    rejects_by_code=rejects_by_code,
                    breaker_state=("open" if breaker_is_open else "closed"),
                    degraded_mode=degraded_mode,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "stage4_run_metrics_persist_failed",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "reason_code": "stage4_run_metrics_persist_failed",
                            "error_type": type(exc).__name__,
                        }
                    },
                )

            try:
                stage4_alert_metrics = build_cycle_metrics(
                    stage4_cycle_summary={
                        "cycle_duration_ms": updated_cycle_duration_ms,
                        "intents_created": len(intents),
                        "intents_executed": execution_report.executed_total,
                        "orders_submitted": execution_report.submitted,
                        "orders_failed": execution_report.rejected,
                        "rejects_by_code": rejects_by_code,
                        "breaker_open": bool((api_snapshot or {}).get("breaker_open", False)),
                        "unknown_order_present": int(counts.get("unknown_closed", 0) > 0),
                        "dryrun_submission_suppressed_total": execution_report.simulated,
                        "dryrun_exchange_degraded_total": int(degraded_mode),
                        "dryrun_market_data_stale_total": int(market_snapshot.dryrun_freshness_stale),
                        "dryrun_market_data_missing_symbols_total": market_snapshot.dryrun_freshness_missing_symbols_count,
                        "dryrun_market_data_age_ms": int(market_snapshot.dryrun_freshness_age_ms or 0),
                        "dryrun_ws_rest_fallback_total": int(market_snapshot.dryrun_ws_rest_fallback_used),
                        "suppressed_by_killswitch_submit": (
                            killswitch_suppressed_counts["submit"]
                            + killswitch_suppressed_counts["replace_submit"]
                        ),
                        "suppressed_by_killswitch_cancel": killswitch_suppressed_counts["cancel"],
                        "suppressed_by_unknown_freeze_submit": (
                            freeze_suppressed_counts["submit"]
                            + freeze_suppressed_counts["replace_submit"]
                        ),
                        "suppressed_by_unknown_freeze_cancel": freeze_suppressed_counts["cancel"],
                    },
                    reconcile_result={
                        "api_429_backoff_total": int((api_snapshot or {}).get("api_429_backoff_total", 0)),
                        "cursor_stall_by_symbol": cursor_stall_by_symbol,
                    },
                    health_snapshot=api_snapshot if isinstance(api_snapshot, dict) else {},
                    final_mode={
                        "mode": final_mode.value,
                        "observe_only": final_mode == Mode.OBSERVE_ONLY,
                        "kill_switch": bool(effective_kill_switch),
                    },
                    cursor_diag=cursor_diag,
                )
                now_epoch = int(datetime.now(UTC).timestamp())
                if settings.dry_run:
                    dryrun_metric_keys = {
                        "dryrun_market_data_stale_total",
                        "dryrun_market_data_missing_symbols_total",
                        "dryrun_market_data_age_ms",
                        "dryrun_ws_rest_fallback_total",
                        "dryrun_exchange_degraded_total",
                        "dryrun_cycle_duration_ms",
                        "dryrun_submission_suppressed_total",
                    }
                    for metric_name in dryrun_metric_keys:
                        metric_value = stage4_alert_metrics.get(metric_name)
                        if metric_value is not None:
                            self._alert_store.record(metric_name, metric_value, now_epoch)

                    stale_stats = self._alert_store.compute("5m", "dryrun_market_data_stale_total")
                    completed_stats = self._alert_store.compute("5m", "dryrun_cycle_completed_total")
                    stale_ratio = 0.0
                    if completed_stats.get("delta", 0.0) > 0:
                        stale_ratio = (
                            stale_stats.get("delta", 0.0)
                            / completed_stats.get("delta", 0.0)
                        )
                    self._alert_store.record(
                        "dryrun_market_data_stale_ratio", stale_ratio, now_epoch
                    )

                for metric_name, metric_value in stage4_alert_metrics.items():
                    self._alert_store.record(metric_name, metric_value, now_epoch)
                alert_rules = list(BASELINE_ALERT_RULES)
                if settings.dry_run:
                    alert_rules.extend(DRY_RUN_ALERT_RULES)
                alert_events = self._alert_evaluator.evaluate_rules(
                    alert_rules,
                    self._alert_store,
                    now_epoch,
                )
                for event in self._alert_dedupe.filter(alert_events):
                    self._alert_notifier.notify(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "alert_eval_failed",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "error_type": type(exc).__name__,
                        }
                    },
                )
            with uow_factory() as uow:
                uow.trace.record_cycle_audit(
                    cycle_id=cycle_id,
                    counts=counts,
                    decisions=decisions,
                    envelope=envelope,
                )
            state_store.set_last_cycle_id(cycle_id)
            if settings.dry_run:
                now_epoch = int(datetime.now(UTC).timestamp())
                cycle_duration_ms = int(
                    (datetime.now(UTC) - cycle_started_monotonic).total_seconds() * 1000
                )
                instrumentation.counter("dryrun_cycle_completed_total", 1)
                instrumentation.histogram("dryrun_cycle_duration_ms", float(cycle_duration_ms))
                self._alert_store.record("dryrun_cycle_completed_total", 1, now_epoch)
                self._alert_store.record("dryrun_cycle_duration_ms", cycle_duration_ms, now_epoch)
                object.__setattr__(self, "_last_cycle_completed_epoch", now_epoch)

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
            reject_breakdown = self._build_rejects_breakdown(
                decision_report.counters,
                execution_report,
            )
            reject_context = self._summary_reject_context(execution_report)
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
                        "rejects_by_code": rejects_by_code,
                        "rejects_breakdown": reject_breakdown,
                        "reject_context": reject_context,
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

    @staticmethod
    def _compute_mark_price_coverage_ratio(
        *,
        covered_symbols: list[str],
        tradeable_symbols_requested: list[str],
    ) -> float:
        return float(len(covered_symbols)) / float(max(1, len(tradeable_symbols_requested)))

    def _choose_mark_price_fallback_symbol(self, active_symbols: list[str]) -> str | None:
        if not active_symbols:
            return None
        normalized = [self.norm(symbol) for symbol in active_symbols]
        if "BTCTRY" in normalized:
            return "BTCTRY"
        return normalized[0]

    def _try_recover_single_mark_price(
        self,
        *,
        exchange: object,
        active_symbols: list[str],
        mark_prices: dict[str, Decimal],
    ) -> MarkPriceSafetyNetResult:
        fallback_symbol = self._choose_mark_price_fallback_symbol(active_symbols)
        if fallback_symbol is None:
            logger.warning(
                "stage4_mark_price_safety_net_failed",
                extra={
                    "extra": {
                        "event": "stage4_mark_price_safety_net_failed",
                        "reason": "no_active_symbols",
                    }
                },
            )
            return MarkPriceSafetyNetResult(success=False, symbol=None)

        base = getattr(exchange, "client", exchange)
        get_orderbook_with_ts = getattr(exchange, "get_orderbook_with_timestamp", None)
        if not callable(get_orderbook_with_ts):
            get_orderbook_with_ts = getattr(base, "get_orderbook_with_timestamp", None)
        get_orderbook = getattr(exchange, "get_orderbook", None)
        if not callable(get_orderbook):
            get_orderbook = getattr(base, "get_orderbook", None)

        if callable(get_orderbook_with_ts):
            def _fetch_orderbook() -> tuple[object, object]:
                bid_raw, ask_raw, _observed_at = get_orderbook_with_ts(fallback_symbol)
                return bid_raw, ask_raw
        elif callable(get_orderbook):
            def _fetch_orderbook() -> tuple[object, object]:
                bid_raw, ask_raw = get_orderbook(fallback_symbol)
                return bid_raw, ask_raw
        else:
            logger.warning(
                "stage4_mark_price_safety_net_failed",
                extra={
                    "extra": {
                        "event": "stage4_mark_price_safety_net_failed",
                        "symbol": fallback_symbol,
                        "reason": "missing_orderbook_method",
                    }
                },
            )
            return MarkPriceSafetyNetResult(success=False, symbol=fallback_symbol)

        try:
            bid_raw, ask_raw = _fetch_orderbook()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stage4_mark_price_safety_net_failed",
                extra={
                    "extra": {
                        "event": "stage4_mark_price_safety_net_failed",
                        "symbol": fallback_symbol,
                        "reason": "orderbook_fetch_failed",
                        "error_type": type(exc).__name__,
                    }
                },
            )
            return MarkPriceSafetyNetResult(success=False, symbol=fallback_symbol)

        bid = self._safe_decimal(bid_raw)
        ask = self._safe_decimal(ask_raw)
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            logger.warning(
                "stage4_mark_price_safety_net_failed",
                extra={
                    "extra": {
                        "event": "stage4_mark_price_safety_net_failed",
                        "symbol": fallback_symbol,
                        "reason": "invalid_orderbook",
                    }
                },
            )
            return MarkPriceSafetyNetResult(success=False, symbol=fallback_symbol)

        mark_prices[self.norm(fallback_symbol)] = (bid + ask) / Decimal("2")
        return MarkPriceSafetyNetResult(success=True, symbol=self.norm(fallback_symbol))

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
        self,
        exchange: object,
        symbols: list[str],
        *,
        cycle_now: datetime,
        settings: Settings | None = None,
    ) -> MarketSnapshot:
        base = getattr(exchange, "client", exchange)
        get_orderbook = getattr(exchange, "get_orderbook", None)
        if not callable(get_orderbook):
            get_orderbook = getattr(base, "get_orderbook", None)
        instrumentation = get_instrumentation()
        missing_symbols_count = 0
        freshness_stale = False
        freshness_age_ms: int | None = None
        ws_rest_fallback_used = False
        if settings is not None and settings.dry_run:
            try:
                market_data_service = MarketDataService(
                    exchange=base,
                    mode=settings.market_data_mode,
                    ws_rest_fallback=settings.ws_market_data_rest_fallback,
                    orderbook_ttl_ms=settings.orderbook_ttl_ms,
                    orderbook_max_staleness_ms=settings.orderbook_max_staleness_ms,
                )
                _bids, freshness = market_data_service.get_best_bids_with_freshness(
                    symbols,
                    max_age_ms=max(1, int(settings.stale_market_data_seconds * 1000)),
                )
                freshness_stale = bool(freshness.is_stale)
                freshness_age_ms = freshness.observed_age_ms
                ws_rest_fallback_used = freshness.source_mode == "rest_fallback"
                if freshness.is_stale:
                    instrumentation.counter("dryrun_market_data_stale_total", 1)
                if freshness.observed_age_ms is not None:
                    instrumentation.histogram(
                        "dryrun_market_data_age_ms", float(freshness.observed_age_ms)
                    )
                missing_symbols_count = len(freshness.missing_symbols)
                if missing_symbols_count > 0:
                    instrumentation.counter(
                        "dryrun_market_data_missing_symbols_total",
                        missing_symbols_count,
                    )
                if ws_rest_fallback_used:
                    instrumentation.counter("dryrun_ws_rest_fallback_total", 1)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dryrun_market_data_freshness_probe_failed",
                    extra={"extra": {"error_type": type(exc).__name__}},
                )
        mark_prices: dict[str, Decimal] = {}
        orderbooks: dict[str, tuple[Decimal, Decimal]] = {}
        spreads_bps: dict[str, Decimal] = {}
        fetch_ages: list[Decimal] = []
        age_by_symbol: dict[str, Decimal] = {}
        fetched_at_by_symbol: dict[str, datetime] = {}
        anomalies: set[str] = set()
        if not callable(get_orderbook):
            return MarketSnapshot(
                mark_prices=mark_prices,
                orderbooks=orderbooks,
                anomalies=set(symbols),
                spreads_bps=spreads_bps,
                age_seconds_by_symbol={self.norm(symbol): Decimal("999999") for symbol in symbols},
                fetched_at_by_symbol={self.norm(symbol): cycle_now for symbol in symbols},
                max_data_age_seconds=Decimal("999999"),
            )

        get_orderbook_with_ts = getattr(exchange, "get_orderbook_with_timestamp", None)
        if not callable(get_orderbook_with_ts):
            get_orderbook_with_ts = getattr(base, "get_orderbook_with_timestamp", None)
        for symbol in symbols:
            normalized = self.norm(symbol)
            observed_at: datetime
            fetch_started_at = datetime.now(UTC)
            try:
                if callable(get_orderbook_with_ts):
                    bid_raw, ask_raw, observed_raw = get_orderbook_with_ts(symbol)
                    if isinstance(observed_raw, datetime):
                        observed_at = observed_raw.astimezone(UTC)
                    else:
                        observed_at = fetch_started_at
                else:
                    bid_raw, ask_raw = get_orderbook(symbol)
                    observed_at = fetch_started_at
            except Exception:  # noqa: BLE001
                anomalies.add(normalized)
                fetch_ages.append(Decimal("999999"))
                age_by_symbol[normalized] = Decimal("999999")
                fetched_at_by_symbol[normalized] = fetch_started_at
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
            age_seconds = Decimal(str(max(0.0, (cycle_now - observed_at).total_seconds())))
            fetch_ages.append(age_seconds)
            age_by_symbol[normalized] = age_seconds
            fetched_at_by_symbol[normalized] = observed_at

        if not fetch_ages:
            max_age = Decimal("999999")
        else:
            max_age = max(fetch_ages)
        if settings is not None and settings.dry_run:
            health_snapshot_fn = getattr(exchange, "health_snapshot", None)
            snapshot = health_snapshot_fn() if callable(health_snapshot_fn) else {}
            degraded = bool(
                (snapshot or {}).get("degraded", False)
                or (snapshot or {}).get("breaker_open", False)
            )
            if degraded:
                instrumentation.counter("dryrun_exchange_degraded_total", 1)
                object.__setattr__(
                    self,
                    "_dryrun_consecutive_exchange_degraded",
                    self._dryrun_consecutive_exchange_degraded + 1,
                )
            else:
                object.__setattr__(self, "_dryrun_consecutive_exchange_degraded", 0)
            now_epoch = int(datetime.now(UTC).timestamp())
            self._alert_store.record(
                "dryrun_exchange_degraded_consecutive",
                float(self._dryrun_consecutive_exchange_degraded),
                now_epoch,
            )
            self._alert_store.record(
                "dryrun_market_data_missing_symbols_total",
                float(missing_symbols_count),
                now_epoch,
            )
        return MarketSnapshot(
            mark_prices=mark_prices,
            orderbooks=orderbooks,
            anomalies=anomalies,
            spreads_bps=spreads_bps,
            age_seconds_by_symbol=age_by_symbol,
            fetched_at_by_symbol=fetched_at_by_symbol,
            max_data_age_seconds=max_age,
            dryrun_freshness_stale=freshness_stale,
            dryrun_freshness_age_ms=freshness_age_ms,
            dryrun_freshness_missing_symbols_count=missing_symbols_count,
            dryrun_ws_rest_fallback_used=ws_rest_fallback_used,
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
        effective_kill_switch = bool(
            getattr(settings, "kill_switch_effective", settings.kill_switch)
        )
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
                "kill_switch": effective_kill_switch,
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
            kill_switch=effective_kill_switch,
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

    @staticmethod
    def _resolve_effective_kill_switch(
        *, settings: Settings, state_store: StateStore, process_role: str
    ) -> tuple[bool, bool, str]:
        settings_kill = bool(settings.kill_switch)
        db_kill, _reason, _until = state_store.get_kill_switch(process_role)
        effective = settings_kill or bool(db_kill)
        if settings_kill and db_kill:
            source = "both"
        elif settings_kill:
            source = "settings"
        elif db_kill:
            source = "db"
        else:
            source = "none"
        return effective, bool(db_kill), source

    @staticmethod
    def _killswitch_action_label(action_type: LifecycleActionType) -> str:
        if action_type == LifecycleActionType.SUBMIT:
            return "submit"
        if action_type == LifecycleActionType.REPLACE:
            return "replace_submit"
        return "cancel"

    def _suppress_actions_for_killswitch(
        self,
        *,
        actions: list[LifecycleAction],
        effective_kill_switch: bool,
        freeze_all: bool,
        process_role: str,
        kill_switch_source: str,
        instrumentation: object,
    ) -> tuple[list[LifecycleAction], dict[str, int]]:
        counters = {"submit": 0, "replace_submit": 0, "cancel": 0}
        if not effective_kill_switch:
            return actions, counters

        allowed: list[LifecycleAction] = []
        for action in actions:
            if action.action_type == LifecycleActionType.CANCEL and not freeze_all:
                allowed.append(action)
                continue
            action_type = self._killswitch_action_label(action.action_type)
            counters[action_type] += 1
            instrumentation.counter(
                "stage4_killswitch_suppressed_total",
                1,
                attrs={"action_type": action_type, "process_role": process_role},
            )
            logger.warning(
                "stage4_killswitch_suppress",
                extra={
                    "extra": {
                        "process_role": process_role,
                        "action_type": action_type,
                        "client_order_id": action.client_order_id,
                        "symbol": action.symbol,
                        "source": kill_switch_source,
                        "freeze_all": freeze_all,
                    }
                },
            )
        return allowed, counters

    def _suppress_actions_for_unknown_freeze(
        self,
        *,
        actions: list[LifecycleAction],
        freeze_active: bool,
        freeze_all: bool,
        process_role: str,
        instrumentation: object,
    ) -> tuple[list[LifecycleAction], dict[str, int]]:
        counters = {"submit": 0, "replace_submit": 0, "cancel": 0}
        if not freeze_active:
            return actions, counters

        allowed: list[LifecycleAction] = []
        for action in actions:
            if action.action_type == LifecycleActionType.CANCEL and not freeze_all:
                allowed.append(action)
                continue
            action_type = self._killswitch_action_label(action.action_type)
            counters[action_type] += 1
            instrumentation.counter(
                "stage4_freeze_suppressed_total",
                1,
                attrs={"action_type": action_type, "process_role": process_role},
            )
            logger.warning(
                "stage4_unknown_freeze_suppress",
                extra={
                    "extra": {
                        "process_role": process_role,
                        "action_type": action_type,
                        "client_order_id": action.client_order_id,
                        "symbol": action.symbol,
                        "freeze_all": freeze_all,
                    }
                },
            )
        return allowed, counters

    def _evaluate_unknown_order_freeze(
        self,
        *,
        settings: Settings,
        state_store: StateStore,
        process_role: str,
        cycle_now: datetime,
        exchange_open_orders: list[Order],
        reconcile_result: object,
        db_open_orders: list[Order],
        instrumentation: object,
    ) -> tuple[object, bool]:
        existing = state_store.stage4_get_freeze(process_role)
        if not bool(getattr(settings, "stage4_unknown_freeze_enabled", True)):
            return existing, False

        missing_client_id = len(getattr(reconcile_result, "external_missing_client_id", []))
        unknown_open_orders = len(getattr(reconcile_result, "import_external", []))
        db_exchange_mismatch = len(getattr(reconcile_result, "mark_unknown_closed", []))
        threshold = max(1, int(getattr(settings, "stage4_unknown_freeze_threshold", 1)))
        persist_cycles = max(1, int(getattr(settings, "stage4_unknown_freeze_persist_cycles", 3)))
        persist_seconds = max(0, int(getattr(settings, "stage4_unknown_freeze_persist_seconds", 30)))

        details = {
            "exchange_open_orders": len(exchange_open_orders),
            "db_open_orders": len(db_open_orders),
            "missing_client_id_count": missing_client_id,
            "unknown_open_orders_count": unknown_open_orders,
            "db_exchange_mismatch_count": db_exchange_mismatch,
            "sample_missing_client_order_ids": [
                (order.exchange_order_id or "missing") for order in getattr(reconcile_result, "external_missing_client_id", [])[:5]
            ],
            "sample_external_client_order_ids": [
                order.client_order_id for order in getattr(reconcile_result, "import_external", [])[:5]
            ],
            "sample_mismatch_client_order_ids": list(getattr(reconcile_result, "mark_unknown_closed", [])[:5]),
            "persist_cycles": 1,
            "last_cycle_ts": cycle_now.isoformat(),
        }
        reason = None
        trigger = False
        prev_details = existing.details if existing.active else {}
        prev_cycles = int(prev_details.get("persist_cycles", 0)) if isinstance(prev_details, dict) else 0

        if missing_client_id > 0:
            reason = "external_missing_client_id"
            trigger = True
        elif unknown_open_orders >= threshold:
            reason = "unknown_open_orders"
            trigger = True
        else:
            mismatch_cycles = prev_cycles + 1 if db_exchange_mismatch > 0 else 0
            details["persist_cycles"] = mismatch_cycles
            if db_exchange_mismatch > 0 and mismatch_cycles >= persist_cycles:
                reason = "db_exchange_mismatch"
                trigger = True
            elif db_exchange_mismatch > 0 and existing.active and existing.since_ts:
                since = datetime.fromisoformat(existing.since_ts)
                if since.tzinfo is None:
                    since = since.replace(tzinfo=UTC)
                if int((cycle_now - since.astimezone(UTC)).total_seconds()) >= persist_seconds:
                    reason = "db_exchange_mismatch"
                    trigger = True

        if not trigger:
            if existing.active:
                heartbeat_details = dict(existing.details) if isinstance(existing.details, dict) else {}
                heartbeat_details["last_cycle_ts"] = cycle_now.isoformat()
                heartbeat_details["exchange_open_orders"] = len(exchange_open_orders)
                heartbeat_details["db_open_orders"] = len(db_open_orders)
                refreshed = state_store.stage4_set_freeze(
                    process_role,
                    reason=str(existing.reason or "unknown_open_orders"),
                    details=heartbeat_details,
                )
                logger.warning(
                    "stage4_unknown_freeze",
                    extra={
                        "extra": {
                            "process_role": process_role,
                            "reason": refreshed.reason,
                            "since": refreshed.since_ts,
                            "details": refreshed.details,
                        }
                    },
                )
                return refreshed, False
            return existing, False

        details["persist_cycles"] = details.get("persist_cycles", prev_cycles + 1)
        freeze_state = state_store.stage4_set_freeze(
            process_role,
            reason=reason or "unknown_open_orders",
            details=details,
        )
        instrumentation.counter(
            "stage4_freeze_trigger_total",
            1,
            attrs={"reason": freeze_state.reason or "unknown", "process_role": process_role},
        )
        logger.warning(
            "stage4_unknown_freeze",
            extra={
                "extra": {
                    "process_role": process_role,
                    "reason": freeze_state.reason,
                    "since": freeze_state.since_ts,
                    "details": freeze_state.details,
                }
            },
        )
        return freeze_state, True

    def _assert_execution_invariant(self, report: object) -> None:
        for field in (
            "executed_total",
            "submitted",
            "canceled",
            "simulated",
            "rejected",
            "rejected_min_notional",
        ):
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
            intent_notional_try = self._resolve_action_intent_notional(action)
            if intent_notional_try is None:
                intent_notional_try = action.price * action.qty
            if notional_try < min_required:
                if intent_notional_try >= min_required and q_price > 0:
                    qty_needed = min_required / q_price
                    q_qty_rescued = Quantizer.quantize_qty_up(qty_needed, rules)
                    rescued_notional_try = q_price * q_qty_rescued
                    if rescued_notional_try >= min_required:
                        logger.info(
                            "stage4_prefilter_min_notional_rescue",
                            extra={
                                "extra": {
                                    "cycle_id": cycle_id,
                                    "symbol": self.norm(action.symbol),
                                    "side": str(action.side),
                                    "client_order_id": action.client_order_id,
                                    "min_required": str(min_required),
                                    "intent_notional_try": str(intent_notional_try),
                                    "before_notional": str(notional_try),
                                    "after_notional": str(rescued_notional_try),
                                    "q_price": str(q_price),
                                    "old_qty": str(q_qty),
                                    "new_qty": str(q_qty_rescued),
                                }
                            },
                        )
                        filtered.append(
                            LifecycleAction(
                                action_type=action.action_type,
                                symbol=action.symbol,
                                side=action.side,
                                price=q_price,
                                qty=q_qty_rescued,
                                reason=action.reason,
                                client_order_id=action.client_order_id,
                                exchange_order_id=action.exchange_order_id,
                                replace_for_client_order_id=action.replace_for_client_order_id,
                            )
                        )
                        continue
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
                            "intent_notional_try": str(intent_notional_try),
                            "required_min_notional_try": str(min_required),
                            "reason_code": "prefilter_min_notional",
                        }
                    },
                )
                continue
            filtered.append(action)
        return filtered, dropped

    def _resolve_action_intent_notional(self, action: LifecycleAction) -> Decimal | None:
        for attr in ("intent_notional_try", "notional_try", "target_notional_try"):
            value = getattr(action, attr, None)
            if value is None:
                continue
            try:
                return Decimal(str(value))
            except Exception:  # noqa: BLE001
                continue
        return None

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
            min_required_notional_try = max(
                Decimal(str(min_order_notional_try)),
                rules.min_notional_try,
            )
            if bootstrap_notional_try <= 0:
                self._inc_reason(drop_reasons, "bootstrap_disabled")
                continue
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

            budget_before_clamp = min(try_cash, Decimal(str(bootstrap_notional_try)))
            budget = max(budget_before_clamp, min_required_notional_try)
            if budget != budget_before_clamp:
                logger.info(
                    "stage4_bootstrap_budget_clamped_to_min_notional",
                    extra={
                        "extra": {
                            "cycle_id": cycle_id,
                            "symbol": normalized,
                            "try_cash": str(try_cash),
                            "bootstrap_setting": str(bootstrap_notional_try),
                            "min_required_notional_try": str(min_required_notional_try),
                            "effective_bootstrap_before": str(budget_before_clamp),
                            "effective_bootstrap_after": str(budget),
                        }
                    },
                )
            if max_notional_per_order_try > 0:
                if max_notional_per_order_try < min_required_notional_try:
                    self._inc_reason(drop_reasons, "max_notional_below_min_notional")
                    logger.info(
                        "stage4_bootstrap_dropped_min_notional",
                        extra={
                            "extra": {
                                "cycle_id": cycle_id,
                                "symbol": normalized,
                                "budget_try": str(max_notional_per_order_try),
                                "min_required_notional_try": str(min_required_notional_try),
                                "reason_code": "max_notional_below_min_notional",
                            }
                        },
                    )
                    continue
                budget = min(budget, max_notional_per_order_try)

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

    def _build_metadata_free_dry_run_bootstrap_intents(
        self,
        *,
        cycle_id: str,
        min_order_notional_try: Decimal,
        bootstrap_notional_try: Decimal,
        symbols: list[str],
        mark_prices: dict[str, Decimal],
        try_cash: Decimal,
        open_orders: list[Order],
        live_mode: bool,
        now_utc: datetime | None = None,
    ) -> list[Order]:
        if live_mode:
            return []

        timestamp = now_utc or datetime.now(UTC)
        existing_buy_symbols = {
            self.norm(order.symbol) for order in open_orders if order.side.lower() == "buy"
        }
        min_notional_floor = Decimal("50")
        min_required_notional_try = max(Decimal(str(min_order_notional_try)), min_notional_floor)
        if bootstrap_notional_try <= 0 and try_cash < min_required_notional_try:
            return []

        for symbol in symbols:
            normalized = self.norm(symbol)
            if normalized in existing_buy_symbols:
                continue
            mark = mark_prices.get(normalized)
            if mark is None or mark <= 0:
                continue
            if try_cash < min_required_notional_try:
                continue

            budget = min(try_cash, Decimal(str(bootstrap_notional_try)))
            budget = max(budget, min_required_notional_try)
            qty = budget / mark
            if qty <= 0:
                continue

            logger.info(
                "stage4_dry_run_metadata_free_bootstrap",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "symbol": normalized,
                        "price": str(mark),
                        "qty": str(qty),
                        "budget_try": str(budget),
                        "reason_code": "metadata_free_bootstrap",
                    }
                },
            )
            return [
                Order(
                    symbol=normalized,
                    side="buy",
                    type="limit",
                    price=mark,
                    qty=qty,
                    status="new",
                    created_at=timestamp,
                    updated_at=timestamp,
                    client_order_id=f"s4-{cycle_id[:12]}-{normalized.lower()}-buy",
                    mode=("live" if live_mode else "dry_run"),
                )
            ]
        return []

    def _persist_dry_run_planned_order(
        self,
        *,
        state_store: StateStore,
        cycle_id: str,
        planned_actions: list[LifecycleAction],
        intents: list[Order],
    ) -> None:
        submit_action = next(
            (action for action in planned_actions if action.action_type == LifecycleActionType.SUBMIT),
            None,
        )
        if submit_action is not None:
            client_order_id = submit_action.client_order_id or (
                f"s4-{cycle_id[:12]}-{self.norm(submit_action.symbol).lower()}-{submit_action.side.lower()}"
            )
            state_store.record_stage4_order_simulated_submit(
                symbol=submit_action.symbol,
                client_order_id=client_order_id,
                side=submit_action.side,
                price=submit_action.price,
                qty=submit_action.qty,
            )
            return

        fallback_intent = next((intent for intent in intents if str(intent.side).lower() == "buy"), None)
        if fallback_intent is None:
            fallback_intent = intents[0] if intents else None
        if fallback_intent is None:
            return

        client_order_id = fallback_intent.client_order_id or (
            f"s4-{cycle_id[:12]}-{self.norm(fallback_intent.symbol).lower()}-{str(fallback_intent.side).lower()}"
        )
        state_store.record_stage4_order_simulated_submit(
            symbol=fallback_intent.symbol,
            client_order_id=client_order_id,
            side=str(fallback_intent.side),
            price=fallback_intent.price,
            qty=fallback_intent.qty,
        )

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
    def _extract_rejects_by_code(counters: Mapping[str, int]) -> dict[str, int]:
        rejects_by_code: dict[str, int] = {}
        for key, value in counters.items():
            match = re.search(r"(?:^|_)rejected(?:_code)?_(\d+)$", str(key))
            if match is None:
                continue
            code = match.group(1)
            rejects_by_code[code] = rejects_by_code.get(code, 0) + int(value)
        return dict(sorted(rejects_by_code.items()))


    @staticmethod
    def _build_rejects_breakdown(
        decision_counters: Mapping[str, int],
        execution_report,
    ) -> dict[str, object]:
        execution_breakdown = dict(getattr(execution_report, "rejects_breakdown", {}) or {})
        if execution_breakdown:
            return {"by_reason": dict(sorted(execution_breakdown.items()))}

        by_signal = {
            key: int(value)
            for key, value in dict(decision_counters).items()
            if key.startswith("rejected") or key.startswith("scaled")
        }
        if by_signal:
            return {"by_signal": dict(sorted(by_signal.items()))}

        if int(getattr(execution_report, "rejected", 0)) > 0:
            details = list(getattr(execution_report, "reject_details", ()) or ())
            if details:
                first = details[0]
                return {
                    "by_reason": {str(first.get("reason", "unknown")): int(getattr(execution_report, "rejected", 0))},
                    "sample": first,
                }
            return {"by_reason": {"unknown": int(getattr(execution_report, "rejected", 0))}}

        return {}

    @staticmethod
    def _summary_reject_context(execution_report) -> dict[str, object]:
        details = list(getattr(execution_report, "reject_details", ()) or ())
        if not details:
            return {}
        primary = dict(details[0])
        return {
            "reason": primary.get("reason", "unknown"),
            "rejected_by_code": primary.get("rejected_by_code", "unknown"),
            "symbol": primary.get("symbol"),
            "side": primary.get("side"),
            "q_price": primary.get("q_price"),
            "q_qty": primary.get("q_qty"),
            "total_try": primary.get("total_try"),
            "min_required_settings": primary.get("min_required_settings"),
            "min_required_exchange_rule": primary.get("min_required_exchange_rule"),
        }

    def _reasons_no_action_enum(
        self,
        *,
        intents_created: int,
        intents_after_risk: int,
        intents_executed: int,
        orders_submitted: int,
        rejects_by_code: Mapping[str, int] | None = None,
        intent_skip_reasons: list[str | None] | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        if intents_created <= 0:
            reasons.append("NO_INTENTS_CREATED")
        if intents_created > 0 and intents_after_risk <= 0:
            reasons.append("ALL_INTENTS_REJECTED_BY_RISK")
        if intents_after_risk > 0 and intents_executed <= 0:
            reasons.append("NO_EXECUTABLE_ACTIONS")
        if intents_executed > 0 and orders_submitted <= 0:
            reasons.append("NO_SUBMISSIONS")
            if int((rejects_by_code or {}).get("1123", 0)) > 0:
                reasons.append("SYMBOL_ON_COOLDOWN_1123")
        if intents_executed <= 0:
            for item in intent_skip_reasons or []:
                mapped = str(item or "").strip().upper()
                if mapped:
                    reasons.append(mapped)
        return [reason for reason in sorted(set(reasons)) if reason in NO_ACTION_REASON_ENUM]

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
    def _safe_json_dict(raw: str | None, *, default: dict[str, object]) -> dict[str, object]:
        if raw is None:
            return dict(default)
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001
            return dict(default)
        if not isinstance(parsed, dict):
            return dict(default)
        return dict(parsed)

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
