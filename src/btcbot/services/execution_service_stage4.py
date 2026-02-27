from __future__ import annotations

# P0.2 diagnostics: standardize Stage4 reject reason labels and attach min-notional numeric context.
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.exchange_stage4 import ExchangeClientStage4
from btcbot.config import Settings
from btcbot.domain.stage4 import (
    LifecycleAction,
    LifecycleActionType,
    Quantizer,
    Stage4RejectReason,
    map_stage4_reject_reason,
)
from btcbot.observability import get_instrumentation
from btcbot.observability_decisions import emit_decision
from btcbot.services.client_order_id_service import build_exchange_client_id
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.execution_wrapper import ExecutionWrapper, UncertainResult
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)

TERMINAL_OLD_ORDER_STATUSES = {"canceled", "filled", "rejected", "unknown_closed"}
REJECT_1123_CODE = 1123
REASON_TOKEN_EXCHANGE_1123 = "exchange_reject_1123"
REASON_TOKEN_GATE_1123 = "exchange_reject_1123_cooldown_gate"
REASON_TOKEN_STATEFAIL_1123 = "exchange_reject_1123_state_read_failed"


@dataclass(frozen=True)
class ExecutionReport:
    """P0.2 diagnostics: keep stable reject reason labels + numeric context for root-cause summaries."""

    executed_total: int
    submitted: int
    canceled: int
    simulated: int
    rejected: int
    rejected_min_notional: int
    rejected_by_code: dict[str, int]
    rejects_breakdown: dict[str, int]
    reject_details: tuple[dict[str, str], ...]


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

    @staticmethod
    def _killswitch_action_label(action_type: LifecycleActionType) -> str:
        if action_type == LifecycleActionType.SUBMIT:
            return "submit"
        if action_type == LifecycleActionType.REPLACE:
            return "replace_submit"
        return "cancel"

    def _record_killswitch_suppression(
        self,
        *,
        action: LifecycleAction,
        process_role: str,
        source: str,
        freeze_all: bool,
    ) -> None:
        action_type = self._killswitch_action_label(action.action_type)
        self.instrumentation.counter(
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
                    "source": source,
                    "freeze_all": freeze_all,
                }
            },
        )


    def _record_freeze_suppression(
        self,
        *,
        action: LifecycleAction,
        process_role: str,
        freeze_reason: str,
        freeze_all: bool,
    ) -> None:
        action_type = self._killswitch_action_label(action.action_type)
        self.instrumentation.counter(
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
                    "reason": freeze_reason,
                    "freeze_all": freeze_all,
                }
            },
        )

    def execute_with_report(self, actions: list[LifecycleAction]) -> ExecutionReport:
        if self.settings.live_trading and not self.settings.is_live_trading_enabled():
            raise RuntimeError("LIVE_TRADING requires LIVE_TRADING_ACK=I_UNDERSTAND")

        live_mode = self.settings.is_live_trading_enabled() and not self.settings.dry_run
        process_role = str(getattr(self.settings, "process_role", "trader"))
        kill_switch_effective = getattr(self.settings, "kill_switch_effective", None)
        kill_switch_active = bool(
            self.settings.kill_switch if kill_switch_effective is None else kill_switch_effective
        )
        kill_switch_source = str(getattr(self.settings, "kill_switch_source", "settings"))
        freeze_all = bool(getattr(self.settings, "kill_switch_freeze_all", False))
        submitted = canceled = simulated = rejected = rejected_min_notional = 0
        self._rejects_by_code: dict[str, int] = {}
        self._rejects_breakdown: dict[str, int] = {}
        self._reject_details: list[dict[str, str]] = []

        freeze_state = self.state_store.stage4_get_freeze(process_role)
        freeze_active = bool(freeze_state.active)
        freeze_reason = str(freeze_state.reason or "freeze_unknown_orders")
        regular_actions, replace_groups = self._extract_replace_groups(actions)

        if kill_switch_active:
            logger.warning(
                "kill_switch_active_stage4",
                extra={
                    "extra": {
                        "process_role": process_role,
                        "source": kill_switch_source,
                        "freeze_all": freeze_all,
                    }
                },
            )
            ks_regular: list[LifecycleAction] = []
            for action in regular_actions:
                if action.action_type == LifecycleActionType.CANCEL and not freeze_all:
                    ks_regular.append(action)
                    continue
                self._record_killswitch_suppression(
                    action=action,
                    process_role=process_role,
                    source=kill_switch_source,
                    freeze_all=freeze_all,
                )
            regular_actions = ks_regular
            ks_replace: list[ReplaceGroup] = []
            for group in replace_groups:
                if freeze_all:
                    for cancel_action in group.cancel_actions:
                        self._record_killswitch_suppression(
                            action=cancel_action,
                            process_role=process_role,
                            source=kill_switch_source,
                            freeze_all=freeze_all,
                        )
                ks_cancels = tuple(
                    cancel_action for cancel_action in group.cancel_actions if not freeze_all
                )
                self._record_killswitch_suppression(
                    action=group.submit_action,
                    process_role=process_role,
                    source=kill_switch_source,
                    freeze_all=freeze_all,
                )
                if ks_cancels:
                    ks_replace.append(
                        ReplaceGroup(
                            symbol=group.symbol,
                            side=group.side,
                            cancel_actions=ks_cancels,
                            submit_action=group.submit_action,
                            submit_count=group.submit_count,
                            had_multiple_submits=group.had_multiple_submits,
                            selected_submit_client_order_id=group.selected_submit_client_order_id,
                        )
                    )
            replace_groups = ks_replace

        if freeze_active:
            fr_regular: list[LifecycleAction] = []
            for action in regular_actions:
                if action.action_type == LifecycleActionType.CANCEL and not freeze_all:
                    fr_regular.append(action)
                    continue
                self._record_freeze_suppression(
                    action=action,
                    process_role=process_role,
                    freeze_reason=freeze_reason,
                    freeze_all=freeze_all,
                )
            regular_actions = fr_regular
            fr_replace: list[ReplaceGroup] = []
            for group in replace_groups:
                if freeze_all:
                    for cancel_action in group.cancel_actions:
                        self._record_freeze_suppression(
                            action=cancel_action,
                            process_role=process_role,
                            freeze_reason=freeze_reason,
                            freeze_all=freeze_all,
                        )
                fr_cancels = tuple(
                    cancel_action for cancel_action in group.cancel_actions if not freeze_all
                )
                self._record_freeze_suppression(
                    action=group.submit_action,
                    process_role=process_role,
                    freeze_reason=freeze_reason,
                    freeze_all=freeze_all,
                )
                if fr_cancels:
                    fr_replace.append(
                        ReplaceGroup(
                            symbol=group.symbol,
                            side=group.side,
                            cancel_actions=fr_cancels,
                            submit_action=group.submit_action,
                            submit_count=group.submit_count,
                            had_multiple_submits=group.had_multiple_submits,
                            selected_submit_client_order_id=group.selected_submit_client_order_id,
                        )
                    )
            replace_groups = fr_replace

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
            if kill_switch_active or freeze_active:
                c = 0
                for cancel_action in group.cancel_actions:
                    c += self._execute_cancel_action(action=cancel_action, live_mode=live_mode)
                s = sim = rej = rej_min = 0
            else:
                s, c, sim, rej, rej_min = self._execute_replace_group(
                    group=group, live_mode=live_mode
                )
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
            rejected_by_code=dict(sorted(self._rejects_by_code.items())),
            rejects_breakdown=dict(sorted(self._rejects_breakdown.items())),
            reject_details=tuple(self._reject_details),
        )

    def _inc_reject_code(self, code: int | str) -> None:
        key = str(code)
        self._rejects_by_code[key] = self._rejects_by_code.get(key, 0) + 1


    def _record_reject_detail(
        self,
        *,
        action: LifecycleAction,
        reason: str,
        rejected_by_code: int | str | None = None,
        min_required_settings: Decimal | None = None,
        min_required_exchange_rule: Decimal | None = None,
        q_price: Decimal | None = None,
        q_qty: Decimal | None = None,
        total_try: Decimal | None = None,
    ) -> None:
        mapped_reason = map_stage4_reject_reason(reject_code=rejected_by_code, reject_token=reason)
        self._rejects_breakdown[mapped_reason] = self._rejects_breakdown.get(mapped_reason, 0) + 1
        detail: dict[str, str] = {
            "reason": mapped_reason,
            "rejected_by_code": str(rejected_by_code if rejected_by_code is not None else "unknown"),
            "symbol": action.symbol,
            "side": action.side,
            "order_type": action.reason,
        }
        if min_required_settings is not None:
            detail["min_required_settings"] = str(min_required_settings)
        if min_required_exchange_rule is not None:
            detail["min_required_exchange_rule"] = str(min_required_exchange_rule)
        if q_price is not None:
            detail["q_price"] = str(q_price)
        if q_qty is not None:
            detail["q_qty"] = str(q_qty)
        if total_try is not None:
            detail["total_try"] = str(total_try)
        self._reject_details.append(detail)

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

        freeze_state = self.state_store.stage4_get_freeze(
            str(getattr(self.settings, "process_role", "trader"))
        )
        if freeze_state.active:
            self.state_store.update_replace_tx_state(
                replace_tx_id=replace_tx_id,
                state="BLOCKED_UNKNOWN",
                last_error="freeze_unknown_orders",
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

    @staticmethod
    def _extract_exchange_error_code(exc: Exception) -> int | None:
        raw_code = getattr(exc, "error_code", None)
        try:
            return int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            return None

    def _get_active_symbol_cooldown(self, symbol: str, now_ts: int):
        getter = getattr(self.state_store, "get_symbol_cooldown", None)
        if not callable(getter):
            return None
        try:
            return getter(symbol=symbol, now_ts=now_ts)
        except Exception:  # noqa: BLE001
            logger.exception("stage4_symbol_cooldown_read_failed", extra={"symbol": symbol})
            return "READ_FAILED"

    def _execute_submit_action(
        self, *, action: LifecycleAction, live_mode: bool
    ) -> tuple[int, int, int, int]:
        freeze_state = self.state_store.stage4_get_freeze(
            str(getattr(self.settings, "process_role", "trader"))
        )
        if freeze_state.active:
            logger.warning(
                "stage4_submit_blocked_due_to_unknown",
                extra={
                    "extra": {
                        "reason": "freeze_unknown_orders",
                        "freeze_reason": freeze_state.reason,
                        "freeze_since": freeze_state.since_ts,
                    }
                },
            )
            return 0, 0, 0, 0
        if not action.client_order_id:
            logger.warning("submit_missing_client_order_id", extra={"symbol": action.symbol})
            return 0, 0, 0, 0

        now_ts = int(datetime.now(UTC).timestamp())
        if self.settings.reject1123_enforce_execution_gate:
            cooldown_state = self._get_active_symbol_cooldown(action.symbol, now_ts)
            if cooldown_state == "READ_FAILED":
                self.state_store.record_stage4_order_rejected(
                    action.client_order_id,
                    REASON_TOKEN_STATEFAIL_1123,
                    symbol=action.symbol,
                    side=action.side,
                    price=action.price,
                    qty=action.qty,
                    mode=("live" if live_mode else "dry_run"),
                    error_code=REJECT_1123_CODE,
                )
                self._inc_reject_code(REJECT_1123_CODE)
                self._record_reject_detail(
                    action=action,
                    reason=REASON_TOKEN_STATEFAIL_1123,
                    rejected_by_code=REJECT_1123_CODE,
                    q_price=action.price,
                    q_qty=action.qty,
                    total_try=action.price * action.qty,
                )
                return 0, 0, 1, 0
            if cooldown_state is not None and cooldown_state.cooldown_until_ts > now_ts:
                self.state_store.record_stage4_order_rejected(
                    action.client_order_id,
                    REASON_TOKEN_GATE_1123,
                    symbol=action.symbol,
                    side=action.side,
                    price=action.price,
                    qty=action.qty,
                    mode=("live" if live_mode else "dry_run"),
                    error_code=REJECT_1123_CODE,
                )
                self._inc_reject_code(REJECT_1123_CODE)
                self._record_reject_detail(
                    action=action,
                    reason=REASON_TOKEN_GATE_1123,
                    rejected_by_code=REJECT_1123_CODE,
                    q_price=action.price,
                    q_qty=action.qty,
                    total_try=action.price * action.qty,
                )
                return 0, 0, 1, 0

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
                self._record_reject_detail(
                    action=action,
                    reason="missing_exchange_rules",
                    q_price=action.price,
                    q_qty=action.qty,
                    total_try=action.price * action.qty,
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
                self._record_reject_detail(
                    action=action,
                    reason="missing_exchange_rules",
                    q_price=action.price,
                    q_qty=action.qty,
                    total_try=action.price * action.qty,
                )
                return 0, 0, 1, 0

        q_price = Quantizer.quantize_price(action.price, rules)
        q_qty = Quantizer.quantize_qty(action.qty, rules)
        min_required_notional = max(
            Decimal(str(self.settings.min_order_notional_try)), rules.min_notional_try
        )
        q_notional = q_price * q_qty
        if q_notional < min_required_notional:
            # Root cause: intent_notional can satisfy min-notional, but floor quantization can push
            # q_price * q_qty below min-required; in that case, ceil qty to step at quantized price.
            intent_notional = action.price * action.qty
            if intent_notional >= min_required_notional and q_price > 0:
                qty_needed = min_required_notional / q_price
                q_qty2 = Quantizer.quantize_qty_up(qty_needed, rules)
                notional2 = q_price * q_qty2
                if notional2 >= min_required_notional:
                    logger.info(
                        "stage4_min_notional_rounding_fix_applied",
                        extra={
                            "extra": {
                                "symbol": action.symbol,
                                "min_required_notional": str(min_required_notional),
                                "q_price": str(q_price),
                                "q_qty_before": str(q_qty),
                                "q_qty_after": str(q_qty2),
                                "notional_before": str(q_notional),
                                "notional_after": str(notional2),
                                "intent_notional": str(intent_notional),
                            }
                        },
                    )
                    q_qty = q_qty2
                    q_notional = notional2

        if q_notional < min_required_notional:
            self.state_store.record_stage4_order_rejected(
                action.client_order_id,
                "min_notional_violation",
                symbol=action.symbol,
                side=action.side,
                price=q_price,
                qty=q_qty,
                mode=("live" if live_mode else "dry_run"),
            )
            self._record_reject_detail(
                action=action,
                reason=Stage4RejectReason.MIN_TOTAL.value,
                min_required_settings=Decimal(str(self.settings.min_order_notional_try)),
                min_required_exchange_rule=rules.min_notional_try,
                q_price=q_price,
                q_qty=q_qty,
                total_try=q_notional,
            )
            emit_decision(
                logger,
                {
                    "event_name": "stage4_submit_rejected",
                    "decision_layer": "execution_stage4",
                    "reason_code": Stage4RejectReason.MIN_TOTAL.value,
                    "action": "REJECT",
                    "payload": self._reject_details[-1],
                },
            )
            return 0, 0, 1, 1
        max_order_notional_cap = self.settings.risk_max_order_notional_try
        if max_order_notional_cap is not None and max_order_notional_cap > 0:
            if q_notional > max_order_notional_cap:
                self.state_store.record_stage4_order_rejected(
                    action.client_order_id,
                    "max_order_notional_try",
                    symbol=action.symbol,
                    side=action.side,
                    price=q_price,
                    qty=q_qty,
                    mode=("live" if live_mode else "dry_run"),
                )
                logger.warning(
                    "stage4_cap_reject",
                    extra={
                        "cap_name": "max_order_notional_try",
                        "cap_value": str(max_order_notional_cap),
                        "notional_try": str(q_notional),
                        "symbol": action.symbol,
                        "side": action.side,
                        "q_price": str(q_price),
                        "q_qty": str(q_qty),
                        "process_role": self.settings.process_role,
                    },
                )
                self.instrumentation.counter(
                    "stage4_cap_reject_total",
                    1,
                    attrs={
                        "cap_name": "max_order_notional_try",
                        "process_role": self.settings.process_role,
                    },
                )
                self._record_reject_detail(
                    action=action,
                    reason="max_order_notional_try",
                    q_price=q_price,
                    q_qty=q_qty,
                    total_try=q_notional,
                )
                return 0, 0, 1, 0

        if not live_mode:
            self.state_store.record_stage4_order_simulated_submit(
                symbol=action.symbol,
                client_order_id=action.client_order_id,
                side=action.side,
                price=q_price,
                qty=q_qty,
            )
            self.instrumentation.counter("dryrun_submission_suppressed_total", 1)
            return 0, 1, 0, 0

        try:
            ack_or_uncertain = self.execution_wrapper.submit_limit_order(
                symbol=action.symbol,
                side=action.side,
                price=q_price,
                qty=q_qty,
                client_order_id=exchange_client_id,
            )
        except Exception as exc:  # noqa: BLE001
            error_code = self._extract_exchange_error_code(exc)
            if error_code == REJECT_1123_CODE:
                reject_state = self.state_store.record_symbol_reject(
                    action.symbol,
                    REJECT_1123_CODE,
                    int(datetime.now(UTC).timestamp()),
                    window_minutes=self.settings.reject1123_window_minutes,
                    threshold=self.settings.reject1123_threshold,
                    cooldown_minutes=self.settings.reject1123_cooldown_minutes,
                )
                self.state_store.record_stage4_order_rejected(
                    action.client_order_id,
                    REASON_TOKEN_EXCHANGE_1123,
                    symbol=action.symbol,
                    side=action.side,
                    price=q_price,
                    qty=q_qty,
                    mode="live",
                    error_code=REJECT_1123_CODE,
                )
                self._inc_reject_code(REJECT_1123_CODE)
                self._record_reject_detail(
                    action=action,
                    reason=REASON_TOKEN_EXCHANGE_1123,
                    rejected_by_code=REJECT_1123_CODE,
                    q_price=q_price,
                    q_qty=q_qty,
                    total_try=q_price * q_qty,
                )
                logger.info(
                    "stage4_exchange_reject_1123_recorded",
                    extra={
                        "extra": {
                            "symbol": action.symbol,
                            "client_order_id": action.client_order_id,
                            "rolling_count": reject_state["rolling_count"],
                            "window_start_ts": reject_state["window_start_ts"],
                            "cooldown_until_ts": reject_state["cooldown_until_ts"],
                        }
                    },
                )
                return 0, 0, 1, 0
            raise
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
