from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from btcbot.adapters.exchange_stage4 import ExchangeClientStage4
from btcbot.config import Settings
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, Quantizer
from btcbot.observability import get_instrumentation
from btcbot.observability_decisions import emit_decision
from btcbot.services.client_order_id_service import build_exchange_client_id
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.execution_wrapper import ExecutionWrapper, UncertainResult
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)

TERMINAL_OLD_ORDER_STATUSES = {"canceled", "filled", "rejected", "unknown_closed"}


@dataclass(frozen=True)
class ExecutionReport:
    executed_total: int
    submitted: int
    canceled: int
    simulated: int
    rejected: int
    rejected_min_notional: int


@dataclass(frozen=True)
class ReplaceGroup:
    symbol: str
    side: str
    cancel_actions: tuple[LifecycleAction, ...]
    submit_action: LifecycleAction
    submit_count: int
    had_multiple_submits: bool
    selected_submit_client_order_id: str | None


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

        regular_actions, replace_groups = self._extract_replace_groups(actions)

        for action in regular_actions:
            if action.action_type == LifecycleActionType.CANCEL:
                canceled += self._execute_cancel_action(action=action, live_mode=live_mode)
                continue
            if action.action_type == LifecycleActionType.SUBMIT:
                s, sim, rej, rej_min = self._execute_submit_action(
                    action=action, live_mode=live_mode
                )
                submitted += s
                simulated += sim
                rejected += rej
                rejected_min_notional += rej_min

        for group in replace_groups:
            if group.had_multiple_submits:
                self.instrumentation.counter("replace_multiple_submits_coalesced_total")
                emit_decision(
                    logger,
                    {
                        "event_name": "replace_multiple_submits_coalesced",
                        "decision_layer": "execution_stage4_replace",
                        "reason_code": "replace_multiple_submits_coalesced",
                        "action": "SUPPRESS",
                        "payload": {
                            "replace_tx_id": self._replace_tx_id(group),
                            "symbol": group.symbol,
                            "side": group.side,
                            "submit_count": group.submit_count,
                            "selected_submit_client_order_id": group.selected_submit_client_order_id,
                        },
                    },
                )
            s, c, sim, rej, rej_min = self._execute_replace_group(group=group, live_mode=live_mode)
            submitted += s
            canceled += c
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

    @staticmethod
    def _extract_replace_groups(
        actions: list[LifecycleAction],
    ) -> tuple[list[LifecycleAction], list[ReplaceGroup]]:
        grouped: dict[tuple[str, str], dict[str, list[LifecycleAction]]] = {}
        for action in actions:
            key = (action.symbol, action.side)
            bucket = grouped.setdefault(key, {"cancel": [], "submit": [], "other": []})
            if (
                action.action_type == LifecycleActionType.CANCEL
                and action.reason == "replace_cancel"
            ):
                bucket["cancel"].append(action)
            elif (
                action.action_type == LifecycleActionType.SUBMIT
                and action.reason == "replace_submit"
            ):
                bucket["submit"].append(action)
            else:
                bucket["other"].append(action)

        regular_actions: list[LifecycleAction] = []
        replace_groups: list[ReplaceGroup] = []
        for (symbol, side), bucket in grouped.items():
            cancels = bucket["cancel"]
            submits = bucket["submit"]
            regular_actions.extend(bucket["other"])
            if cancels and submits:
                selected_submit = submits[-1]
                replace_groups.append(
                    ReplaceGroup(
                        symbol=symbol,
                        side=side,
                        cancel_actions=tuple(cancels),
                        submit_action=selected_submit,
                        submit_count=len(submits),
                        had_multiple_submits=len(submits) > 1,
                        selected_submit_client_order_id=selected_submit.client_order_id,
                    )
                )
            else:
                regular_actions.extend(cancels)
                regular_actions.extend(submits)
        return regular_actions, replace_groups

    @staticmethod
    def _replace_tx_id(group: ReplaceGroup) -> str:
        old_ids = sorted([a.client_order_id or "missing" for a in group.cancel_actions])
        payload = "|".join(
            [group.symbol, group.side, group.submit_action.client_order_id or "missing", *old_ids]
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"rpl:{digest}"

    def _execute_replace_group(
        self, *, group: ReplaceGroup, live_mode: bool
    ) -> tuple[int, int, int, int, int]:
        submitted = canceled = simulated = rejected = rejected_min = 0
        replace_tx_id = self._replace_tx_id(group)
        old_ids = [a.client_order_id for a in group.cancel_actions if a.client_order_id]
        new_id = group.submit_action.client_order_id
        if not new_id or not old_ids:
            self.instrumentation.counter("replace_tx_failed_total")
            return 0, 0, 0, 0, 0

        existing_tx = self.state_store.get_replace_tx(replace_tx_id)
        if existing_tx is None:
            self.state_store.upsert_replace_tx(
                replace_tx_id=replace_tx_id,
                symbol=group.symbol,
                side=group.side,
                old_client_order_ids=old_ids,
                new_client_order_id=new_id,
                state="INIT",
            )
            self.instrumentation.counter("replace_tx_started_total")
            current_state = "INIT"
        else:
            current_state = existing_tx.state
            if (
                existing_tx.symbol.replace("_", "") != group.symbol.replace("_", "")
                or existing_tx.side != group.side
                or set(existing_tx.old_client_order_ids) != set(old_ids)
                or existing_tx.new_client_order_id != new_id
            ):
                self.state_store.update_replace_tx_state(
                    replace_tx_id=replace_tx_id,
                    state=current_state,
                    last_error="replace_tx_metadata_mismatch",
                )
                self.instrumentation.counter("replace_tx_metadata_mismatch_total", 1)
                emit_decision(
                    logger,
                    {
                        "decision_layer": "execution_stage4_replace",
                        "reason_code": "replace_tx_metadata_mismatch",
                        "action": "SUPPRESS",
                        "payload": {
                            "replace_tx_id": replace_tx_id,
                            "symbol": group.symbol,
                            "side": group.side,
                            "old_client_order_ids": old_ids,
                            "new_client_order_id": new_id,
                        },
                    },
                )
                return 0, 0, 0, 0, 0

        if current_state in {"SUBMIT_CONFIRMED", "FAILED"}:
            return 0, 0, 0, 0, 0

        if self.state_store.stage4_has_unknown_orders():
            self.state_store.update_replace_tx_state(
                replace_tx_id=replace_tx_id,
                state="BLOCKED_UNKNOWN",
                last_error="unknown_order_freeze",
            )
            self.instrumentation.counter("replace_tx_blocked_unknown_total")
            emit_decision(
                logger,
                {
                    "decision_layer": "execution_stage4_replace",
                    "reason_code": "replace_deferred_unknown_order_freeze",
                    "action": "SUPPRESS",
                    "payload": {
                        "replace_tx_id": replace_tx_id,
                        "symbol": group.symbol,
                        "side": group.side,
                        "old_client_order_ids": old_ids,
                        "new_client_order_id": new_id,
                    },
                },
            )
            return 0, 0, 0, 0, 0

        for cancel_action in group.cancel_actions:
            canceled += self._execute_cancel_action(action=cancel_action, live_mode=live_mode)
        self.state_store.update_replace_tx_state(replace_tx_id=replace_tx_id, state="CANCEL_SENT")

        confirmed, reason = self._confirm_replace_cancels(group)
        if not confirmed:
            self.state_store.update_replace_tx_state(
                replace_tx_id=replace_tx_id,
                state="BLOCKED_RECONCILE",
                last_error=reason,
            )
            self.instrumentation.counter("replace_tx_deferred_total")
            emit_decision(
                logger,
                {
                    "decision_layer": "execution_stage4_replace",
                    "reason_code": "replace_deferred_cancel_unconfirmed",
                    "action": "SUPPRESS",
                    "payload": {
                        "replace_tx_id": replace_tx_id,
                        "symbol": group.symbol,
                        "side": group.side,
                        "old_client_order_ids": old_ids,
                        "new_client_order_id": new_id,
                        "detail": reason,
                        "reason_code": reason.split(":", 1)[0],
                    },
                },
            )
            return 0, canceled, 0, 0, 0

        self.state_store.update_replace_tx_state(
            replace_tx_id=replace_tx_id, state="CANCEL_CONFIRMED"
        )
        self.state_store.update_replace_tx_state(replace_tx_id=replace_tx_id, state="SUBMIT_SENT")
        submit_action = LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol=group.submit_action.symbol,
            side=group.submit_action.side,
            price=group.submit_action.price,
            qty=group.submit_action.qty,
            reason=group.submit_action.reason,
            client_order_id=group.submit_action.client_order_id,
        )
        s, sim, rej, rej_min = self._execute_submit_action(
            action=submit_action, live_mode=live_mode
        )
        submitted += s
        simulated += sim
        rejected += rej
        rejected_min += rej_min

        if submitted or simulated:
            self.state_store.update_replace_tx_state(
                replace_tx_id=replace_tx_id, state="SUBMIT_CONFIRMED"
            )
            self.instrumentation.counter("replace_tx_committed_total")
            emit_decision(
                logger,
                {
                    "decision_layer": "execution_stage4_replace",
                    "reason_code": "replace_committed",
                    "action": "ALLOW",
                    "payload": {
                        "replace_tx_id": replace_tx_id,
                        "symbol": group.symbol,
                        "side": group.side,
                        "old_client_order_ids": old_ids,
                        "new_client_order_id": new_id,
                    },
                },
            )
        elif rejected:
            self.state_store.update_replace_tx_state(
                replace_tx_id=replace_tx_id,
                state="FAILED",
                last_error="submit_rejected",
            )
            self.instrumentation.counter("replace_tx_failed_total")

        return submitted, canceled, simulated, rejected, rejected_min

    def _confirm_replace_cancels(self, group: ReplaceGroup) -> tuple[bool, str]:
        try:
            open_orders = self.exchange.list_open_orders(group.symbol)
        except Exception as exc:  # noqa: BLE001
            return False, f"reconcile_failed:{type(exc).__name__}"

        open_client_ids = {o.client_order_id for o in open_orders if o.client_order_id}
        for cancel_action in group.cancel_actions:
            old_id = cancel_action.client_order_id
            if not old_id:
                continue
            if old_id in open_client_ids:
                return False, f"old_id_still_open:{old_id}"
            local_order = self.state_store.get_stage4_order_by_client_id(old_id)
            if local_order is None or local_order.status not in TERMINAL_OLD_ORDER_STATUSES:
                status = "missing" if local_order is None else local_order.status
                return (
                    False,
                    f"local_missing_record:{old_id}"
                    if status == "missing"
                    else f"local_state_not_terminal:{old_id}:{status}",
                )

        return True, "confirmed"

    def _execute_submit_action(
        self, *, action: LifecycleAction, live_mode: bool
    ) -> tuple[int, int, int, int]:
        if self.state_store.stage4_has_unknown_orders():
            logger.warning(
                "stage4_submit_blocked_due_to_unknown",
                extra={
                    "extra": {"unknown_orders": self.state_store.stage4_unknown_client_order_ids()}
                },
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
            logger.info("submit_deduped")
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
        if not Quantizer.validate_min_notional(q_price, q_qty, rules):
            self.state_store.record_stage4_order_rejected(
                action.client_order_id,
                "min_notional_violation",
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
        return 1 if canceled_or_uncertain else 0
