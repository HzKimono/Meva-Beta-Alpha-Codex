from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from btcbot.adapters.action_to_order import build_exchange_rules
from btcbot.adapters.btcturk_http import ConfigurationError
from btcbot.config import Settings
from btcbot.domain.models import PairInfo, normalize_symbol
from btcbot.domain.stage4 import Order, Position, Quantizer
from btcbot.domain.strategy_core import PositionSummary
from btcbot.services import metrics_service
from btcbot.services.accounting_service_stage4 import AccountingService
from btcbot.services.decision_pipeline_service import DecisionPipelineService
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.execution_service_stage4 import ExecutionService
from btcbot.services.ledger_service import LedgerService
from btcbot.services.metrics_service import CycleMetrics
from btcbot.services.order_lifecycle_service import OrderLifecycleService
from btcbot.services.reconcile_service import ReconcileService
from btcbot.services.risk_policy import RiskPolicy
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)


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
        cycle_id = uuid4().hex
        cycle_started_at = datetime.now(UTC)

        envelope = {
            "cycle_id": cycle_id,
            "command": self.command,
            "dry_run": settings.dry_run,
            "live_mode": live_mode,
            "symbols": sorted(self.norm(symbol) for symbol in settings.symbols),
            "timestamp_utc": datetime.now(UTC).isoformat(),
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
            execution_service = ExecutionService(
                exchange=exchange,
                state_store=state_store,
                settings=settings,
                rules_service=rules_service,
            )
            decision_pipeline = DecisionPipelineService(settings=settings)

            mark_prices, mark_price_errors = self._resolve_mark_prices(exchange, settings.symbols)
            try_cash = self._resolve_try_cash(
                exchange, fallback=Decimal(str(settings.dry_run_try_balance))
            )

            exchange_open_orders: list[Order] = []
            open_order_failures = 0
            failed_symbols: set[str] = set(mark_price_errors)
            for symbol in settings.symbols:
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
                for symbol in settings.symbols
            }
            cursor_after_by_symbol: dict[str, str] = {}
            for symbol in settings.symbols:
                normalized = self.norm(symbol)
                try:
                    fetched = accounting_service.fetch_new_fills(symbol)
                    fills.extend(fetched.fills)
                    fills_fetched += len(fetched.fills)
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
                for symbol in settings.symbols
            }
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

            pnl_report = ledger_service.report(mark_prices=mark_prices, cash_try=try_cash)
            current_open_orders = state_store.list_stage4_open_orders()
            positions = state_store.list_stage4_positions()
            positions_by_symbol = {self.norm(position.symbol): position for position in positions}
            pair_info = self._resolve_pair_info(exchange)

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
                bootstrap_enabled=settings.stage4_bootstrap_intents,
                live_mode=live_mode,
            )
            pipeline_orders = [
                order
                for order in decision_report.order_requests
                if self.norm(order.symbol) not in failed_symbols
            ]
            bootstrap_intents, bootstrap_drop_reasons = self._build_intents(
                cycle_id=cycle_id,
                symbols=[
                    symbol for symbol in settings.symbols if self.norm(symbol) not in failed_symbols
                ],
                mark_prices=mark_prices,
                try_cash=try_cash,
                open_orders=current_open_orders,
                live_mode=live_mode,
                bootstrap_enabled=settings.stage4_bootstrap_intents,
                pair_info=pair_info,
            )
            intents = pipeline_orders or bootstrap_intents
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

            execution_report = execution_service.execute_with_report(accepted_actions)
            self._assert_execution_invariant(execution_report)
            cycle_metrics: CycleMetrics = metrics_service.build_cycle_metrics(
                cycle_id=cycle_id,
                cycle_started_at=cycle_started_at,
                cycle_ended_at=datetime.now(UTC),
                mode=("NORMAL" if live_mode else "OBSERVE_ONLY"),
                fills=fills,
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
                "fills_applied": len(fills),
                "cursor_before": sum(1 for value in cursor_before.values() if value is not None),
                "cursor_after": sum(1 for value in cursor_after.values() if value is not None),
                "planned_actions": len(lifecycle_plan.actions),
                "pipeline_intents": len(decision_report.intents),
                "pipeline_order_requests": len(decision_report.order_requests),
                "pipeline_mapped_orders": decision_report.mapped_orders_count,
                "pipeline_dropped_actions": decision_report.dropped_actions_count,
                "bootstrap_mapped_orders": len(bootstrap_intents),
                "bootstrap_dropped_symbols": sum(bootstrap_drop_reasons.values()),
                "accepted_actions": len(accepted_actions),
                "executed": execution_report.executed_total,
                "submitted": execution_report.submitted,
                "canceled": execution_report.canceled,
                "rejected_min_notional": execution_report.rejected,
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
                            "cycle_id": cycle_id,
                            "fills_per_submitted_order": cycle_metrics.fills_per_submitted_order,
                            "fees": cycle_metrics.fees,
                            "pnl": cycle_metrics.pnl,
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

    def _resolve_mark_prices(
        self, exchange: object, symbols: list[str]
    ) -> tuple[dict[str, Decimal], set[str]]:
        base = getattr(exchange, "client", exchange)
        get_orderbook = getattr(base, "get_orderbook", None)
        mark_prices: dict[str, Decimal] = {}
        anomalies: set[str] = set()
        if not callable(get_orderbook):
            return mark_prices, anomalies

        for symbol in symbols:
            normalized = self.norm(symbol)
            try:
                bid_raw, ask_raw = get_orderbook(symbol)
            except Exception:  # noqa: BLE001
                anomalies.add(normalized)
                continue
            bid = self._safe_decimal(bid_raw)
            ask = self._safe_decimal(ask_raw)
            if bid is not None and ask is not None and ask < bid:
                bid, ask = ask, bid
                anomalies.add(normalized)
            if bid is not None and bid > 0 and ask is not None and ask > 0:
                mark = (bid + ask) / Decimal("2")
            elif bid is not None and bid > 0:
                mark = bid
            elif ask is not None and ask > 0:
                mark = ask
            else:
                anomalies.add(normalized)
                continue
            mark_prices[normalized] = mark
        return mark_prices, anomalies

    def _assert_execution_invariant(self, report: object) -> None:
        for field in ("executed_total", "submitted", "canceled", "simulated", "rejected"):
            if not hasattr(report, field):
                raise Stage4InvariantError(f"execution_report_missing_{field}")
            value = getattr(report, field)
            if not isinstance(value, int) or value < 0:
                raise Stage4InvariantError(f"execution_report_invalid_{field}")

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
        symbols: list[str],
        mark_prices: dict[str, Decimal],
        try_cash: Decimal,
        open_orders: list[Order],
        live_mode: bool,
        bootstrap_enabled: bool,
        pair_info: list[PairInfo] | None,
    ) -> tuple[list[Order], dict[str, int]]:
        if not bootstrap_enabled:
            return [], {}

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
                continue

            budget = min(try_cash, Decimal("50"))
            if budget <= 0:
                continue
            qty_raw = budget / mark
            if qty_raw <= 0:
                continue

            rules = build_exchange_rules(rules_source)
            price_q = Quantizer.quantize_price(mark, rules)
            qty_q = Quantizer.quantize_qty(qty_raw, rules)
            if qty_q <= 0:
                self._inc_reason(drop_reasons, "qty_became_zero")
                continue
            if not Quantizer.validate_min_notional(price_q, qty_q, rules):
                self._inc_reason(drop_reasons, "min_notional")
                continue

            intents.append(
                Order(
                    symbol=symbol,
                    side="buy",
                    type="limit",
                    price=price_q,
                    qty=qty_q,
                    status="new",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    client_order_id=f"s4-{cycle_id[:12]}-{normalized.lower()}-buy",
                    mode=("live" if live_mode else "dry_run"),
                )
            )
        return intents, drop_reasons

    @staticmethod
    def _inc_reason(reasons: dict[str, int], key: str) -> None:
        reasons[key] = reasons.get(key, 0) + 1
