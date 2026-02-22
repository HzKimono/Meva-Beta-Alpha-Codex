from __future__ import annotations

import logging
from dataclasses import dataclass

from btcbot.adapters.exchange_stage4 import ExchangeClientStage4
from btcbot.config import Settings
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, Quantizer
from btcbot.observability import get_instrumentation
from btcbot.observability_decisions import emit_decision
from btcbot.services.client_order_id_service import build_exchange_client_id
from btcbot.services.execution_wrapper import ExecutionWrapper, UncertainResult
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionReport:
    executed_total: int
    submitted: int
    canceled: int
    simulated: int
    rejected: int
    rejected_min_notional: int


class ExecutionService:
    """Stage 4 execution gate. Only this service performs exchange write side effects."""

    def __init__(
        self,
        exchange: ExchangeClientStage4,
        state_store: StateStore,
        settings: Settings,
        rules_service: ExchangeRulesService,
    ) -> None:
        self.exchange = exchange
        self.state_store = state_store
        self.settings = settings
        self.rules_service = rules_service
        self.execution_wrapper = ExecutionWrapper(exchange)
        self.instrumentation = get_instrumentation()

    def execute(self, actions: list[LifecycleAction]) -> int:
        return self.execute_with_report(actions).executed_total

    def execute_with_report(self, actions: list[LifecycleAction]) -> ExecutionReport:
        if self.settings.kill_switch:
            logger.warning("kill_switch_active_blocking_writes")
            return ExecutionReport(0, 0, 0, 0, 0, 0)
        if self.settings.live_trading and not self.settings.is_live_trading_enabled():
            raise RuntimeError("LIVE_TRADING requires LIVE_TRADING_ACK=I_UNDERSTAND")

        live_mode = self.settings.is_live_trading_enabled() and not self.settings.dry_run
        submitted = canceled = simulated = rejected = rejected_min_notional = 0

        for action in actions:
            if action.action_type == LifecycleActionType.CANCEL:
                canceled += self._execute_cancel_action(action=action, live_mode=live_mode)
                continue
            if action.action_type != LifecycleActionType.SUBMIT:
                continue
            if self._is_replace_submit(action):
                s, sim, rej, rej_min = self._execute_replace_submit(action=action, live_mode=live_mode)
            else:
                s, sim, rej, rej_min = self._execute_submit_action(action=action, live_mode=live_mode)
            submitted += s
            simulated += sim
            rejected += rej
            rejected_min_notional += rej_min

        return ExecutionReport(
            executed_total=submitted + canceled + simulated,
            submitted=submitted,
            canceled=canceled,
            simulated=simulated,
            rejected=rejected,
            rejected_min_notional=rejected_min_notional,
        )

    def _execute_submit_action(self, *, action: LifecycleAction, live_mode: bool) -> tuple[int, int, int, int]:
        if self.state_store.stage4_has_unknown_orders():
            logger.warning(
                "stage4_submit_blocked_due_to_unknown",
                extra={"extra": {"unknown_orders": self.state_store.stage4_unknown_client_order_ids()}},
            )
            return 0, 0, 0, 0
        if not action.client_order_id:
            logger.warning("submit_missing_client_order_id", extra={"symbol": action.symbol})
            return 0, 0, 0, 0

        exchange_client_id = build_exchange_client_id(
            internal_client_id=action.client_order_id,
            symbol=action.symbol,
            side=action.side,
        )
        dedupe_decision = self.state_store.stage4_submit_dedupe_status(
            internal_client_order_id=action.client_order_id,
            exchange_client_order_id=exchange_client_id,
        )
        if dedupe_decision.should_dedupe:
            logger.info(
                "submit_deduped",
                extra={
                    "extra": {
                        "internal_client_order_id": action.client_order_id,
                        "exchange_client_order_id": exchange_client_id,
                        "dedupe_key": dedupe_decision.dedupe_key,
                        "reason": dedupe_decision.reason,
                        "age_s": dedupe_decision.age_seconds,
                        "related_order_id": dedupe_decision.related_order_id,
                        "related_status": dedupe_decision.related_status,
                    }
                },
            )
            return 0, 0, 0, 0

        resolve_boundary = getattr(self.rules_service, "resolve_boundary", None)
        if callable(resolve_boundary):
            decision = resolve_boundary(action.symbol)
            if decision.rules is None:
                self.state_store.record_stage4_order_rejected(
                    action.client_order_id,
                    f"missing_exchange_rules:{decision.resolution.status}",
                    symbol=action.symbol,
                    side=action.side,
                    price=action.price,
                    qty=action.qty,
                    mode=("live" if live_mode else "dry_run"),
                )
                return 0, 0, 1, 0
            rules = decision.rules
        else:
            try:
                rules = self.rules_service.get_rules(action.symbol)
            except ValueError:
                self.state_store.record_stage4_order_rejected(
                    action.client_order_id,
                    "missing_exchange_rules",
                    symbol=action.symbol,
                    side=action.side,
                    price=action.price,
                    qty=action.qty,
                    mode=("live" if live_mode else "dry_run"),
                )
                return 0, 0, 1, 0

        q_price = Quantizer.quantize_price(action.price, rules)
        q_qty = Quantizer.quantize_qty(action.qty, rules)
        order_notional_try = q_price * q_qty
        if not Quantizer.validate_min_notional(q_price, q_qty, rules):
            reason = (
                "min_notional_violation:"
                f"notional_try={order_notional_try}:"
                f"required_try={rules.min_notional_try}"
            )
            logger.info(
                "submit_rejected_min_notional",
                extra={
                    "extra": {
                        "symbol": action.symbol,
                        "side": action.side,
                        "client_order_id": action.client_order_id,
                        "order_notional_try": str(order_notional_try),
                        "required_min_notional_try": str(rules.min_notional_try),
                    }
                },
            )
            self.state_store.record_stage4_order_rejected(
                action.client_order_id,
                reason,
                symbol=action.symbol,
                side=action.side,
                price=q_price,
                qty=q_qty,
                mode=("live" if live_mode else "dry_run"),
            )
            return 0, 0, 1, 1

        if not live_mode:
            self.state_store.record_stage4_order_simulated_submit(
                symbol=action.symbol,
                client_order_id=action.client_order_id,
                side=action.side,
                price=q_price,
                qty=q_qty,
            )
            return 0, 1, 0, 0

        ack_or_uncertain = self.execution_wrapper.submit_limit_order(
            symbol=action.symbol,
            side=action.side,
            price=q_price,
            qty=q_qty,
            client_order_id=exchange_client_id,
        )
        if isinstance(ack_or_uncertain, UncertainResult):
            self.state_store.record_stage4_order_error(
                client_order_id=action.client_order_id,
                reason="submit_uncertain_outcome",
                symbol=action.symbol,
                side=action.side,
                price=q_price,
                qty=q_qty,
                mode="live",
                status="unknown",
            )
            return 0, 0, 0, 0

        ack = ack_or_uncertain
        logger.info(
            "submit_acknowledged",
            extra={
                "extra": {
                    "internal_client_order_id": action.client_order_id,
                    "exchange_client_order_id": exchange_client_id,
                    "exchange_order_id": ack.exchange_order_id,
                }
            },
        )
        self.state_store.record_stage4_order_submitted(
            symbol=action.symbol,
            client_order_id=action.client_order_id,
            exchange_client_id=exchange_client_id,
            exchange_order_id=ack.exchange_order_id,
            side=action.side,
            price=q_price,
            qty=q_qty,
            mode="live",
            status="open",
        )
        return 1, 0, 0, 0

    def _execute_cancel_action(self, *, action: LifecycleAction, live_mode: bool) -> int:
        client_id = action.client_order_id
        if not client_id:
            logger.warning("cancel_missing_client_id")
            return 0
        if self.state_store.is_order_terminal(client_id):
            return 0

        order = self.state_store.get_stage4_order_by_client_id(client_id)
        exchange_id = action.exchange_order_id or (order.exchange_order_id if order else None)
        if exchange_id is None:
            self.state_store.record_stage4_order_error(
                client_order_id=client_id,
                reason="cancel_missing_exchange_order_id",
                symbol=action.symbol,
                side=action.side,
                price=action.price,
                qty=action.qty,
                mode=("live" if live_mode else "dry_run"),
                status="error",
            )
            return 0

        self.state_store.record_stage4_order_cancel_requested(client_id)
        if not live_mode:
            self.state_store.record_stage4_order_canceled(client_id)
            return 1

        canceled_or_uncertain = self.execution_wrapper.cancel_order(exchange_order_id=exchange_id)
        if isinstance(canceled_or_uncertain, UncertainResult):
            self.state_store.record_stage4_order_error(
                client_order_id=client_id,
                reason="cancel_uncertain_outcome",
                symbol=action.symbol,
                side=action.side,
                price=action.price,
                qty=action.qty,
                mode="live",
                status="unknown",
            )
            return 0
        if canceled_or_uncertain:
            self.state_store.record_stage4_order_canceled(client_id)
            return 1
        return 0

    def _is_replace_submit(self, action: LifecycleAction) -> bool:
        return action.reason == "replace_submit" or action.replace_for_client_order_id is not None

    def _execute_replace_submit(self, *, action: LifecycleAction, live_mode: bool) -> tuple[int, int, int, int]:
        if not action.client_order_id or not action.replace_for_client_order_id:
            self.state_store.record_stage4_order_error(
                client_order_id=action.client_order_id or "missing-client-order-id",
                reason="replace_missing_linkage",
                symbol=action.symbol,
                side=action.side,
                price=action.price,
                qty=action.qty,
                mode=("live" if live_mode else "dry_run"),
                status="error",
            )
            self.instrumentation.counter("stage4_replace_missing_linkage_total")
            return 0, 0, 0, 0

        replace_client_id = action.client_order_id
        old_client_id = action.replace_for_client_order_id
        self.state_store.upsert_stage4_replace_transaction(
            new_client_order_id=replace_client_id,
            old_client_order_id=old_client_id,
            symbol=action.symbol,
            side=action.side,
            status="pending_cancel",
        )

        unknown_orders = self.state_store.stage4_unknown_client_order_ids()
        if unknown_orders:
            self.state_store.upsert_stage4_replace_transaction(
                new_client_order_id=replace_client_id,
                old_client_order_id=old_client_id,
                symbol=action.symbol,
                side=action.side,
                status="blocked_unknown",
                last_error="unknown_order_freeze",
            )
            self.instrumentation.counter("stage4_replace_blocked_unknown_total")
            emit_decision(
                logger,
                {
                    "decision_layer": "execution_stage4_replace",
                    "reason_code": "replace_blocked_unknown_order_freeze",
                    "action": "SUPPRESS",
                    "payload": {
                        "replace_client_order_id": replace_client_id,
                        "replace_for_client_order_id": old_client_id,
                        "unknown_orders": unknown_orders,
                    },
                },
            )
            return 0, 0, 0, 0

        old_order = self.state_store.get_stage4_order_by_client_id(old_client_id)
        if old_order is not None and old_order.status not in {"canceled", "filled", "rejected", "unknown_closed"}:
            self.state_store.upsert_stage4_replace_transaction(
                new_client_order_id=replace_client_id,
                old_client_order_id=old_client_id,
                symbol=action.symbol,
                side=action.side,
                status="pending_cancel",
                last_error=f"cancel_not_confirmed:{old_order.status}",
            )
            self.instrumentation.counter("stage4_replace_waiting_cancel_total")
            self.instrumentation.gauge("stage4_replace_inflight", 1.0)
            emit_decision(
                logger,
                {
                    "decision_layer": "execution_stage4_replace",
                    "reason_code": "replace_waiting_cancel_confirmation",
                    "action": "SUPPRESS",
                    "payload": {
                        "replace_client_order_id": replace_client_id,
                        "replace_for_client_order_id": old_client_id,
                        "old_status": old_order.status,
                    },
                },
            )
            return 0, 0, 0, 0

        submitted, simulated, rejected, rejected_min = self._execute_submit_action(
            action=LifecycleAction(
                action_type=LifecycleActionType.SUBMIT,
                symbol=action.symbol,
                side=action.side,
                price=action.price,
                qty=action.qty,
                reason="replace_submit_atomic",
                client_order_id=replace_client_id,
            ),
            live_mode=live_mode,
        )
        if submitted or simulated:
            self.state_store.upsert_stage4_replace_transaction(
                new_client_order_id=replace_client_id,
                old_client_order_id=old_client_id,
                symbol=action.symbol,
                side=action.side,
                status="submitted",
                last_error=None,
            )
            self.instrumentation.counter("stage4_replace_committed_total")
            self.instrumentation.gauge("stage4_replace_inflight", 0.0)
            emit_decision(
                logger,
                {
                    "decision_layer": "execution_stage4_replace",
                    "reason_code": "replace_committed",
                    "action": "ALLOW",
                    "payload": {
                        "replace_client_order_id": replace_client_id,
                        "replace_for_client_order_id": old_client_id,
                    },
                },
            )
        return submitted, simulated, rejected, rejected_min
