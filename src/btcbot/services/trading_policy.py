from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from btcbot.domain.decision_codes import ReasonCode
from btcbot.observability_decisions import emit_decision


class PolicyBlockReason(StrEnum):
    MONITOR_ROLE = "MONITOR_ROLE"
    KILL_SWITCH = "KILL_SWITCH"
    DRY_RUN = "DRY_RUN"
    NOT_ARMED = "NOT_ARMED"
    ACK_MISSING = "ACK_MISSING"


@dataclass(frozen=True)
class LiveSideEffectsPolicyResult:
    allowed: bool
    reasons: list[str]
    message: str


POLICY_REASON_TO_CODE: dict[PolicyBlockReason, ReasonCode] = {
    PolicyBlockReason.MONITOR_ROLE: ReasonCode.POLICY_BLOCK_MONITOR_ROLE,
    PolicyBlockReason.KILL_SWITCH: ReasonCode.POLICY_BLOCK_KILL_SWITCH,
    PolicyBlockReason.DRY_RUN: ReasonCode.POLICY_BLOCK_DRY_RUN,
    PolicyBlockReason.NOT_ARMED: ReasonCode.POLICY_BLOCK_NOT_ARMED,
    PolicyBlockReason.ACK_MISSING: ReasonCode.POLICY_BLOCK_ACK_MISSING,
}


def policy_reason_to_code(reason: PolicyBlockReason | str) -> ReasonCode:
    normalized = PolicyBlockReason(reason)
    return POLICY_REASON_TO_CODE[normalized]


def validate_live_side_effects_policy(
    *,
    process_role: str | None = None,
    enforce_monitor_role: bool = False,
    dry_run: bool,
    kill_switch: bool,
    live_trading_enabled: bool,
    live_trading_ack: bool,
    cycle_id: str | None = None,
    logger=None,
    decision_layer: str = "policy_gate",
    action: str = "BLOCK",
    scope: str = "global",
    symbol: str | None = None,
    side: str | None = None,
    intent_id: str | None = None,
) -> LiveSideEffectsPolicyResult:
    reasons: list[str] = []
    message_fragments: list[str] = []
    role_normalized = (process_role or "").strip().upper()

    if enforce_monitor_role and role_normalized == "MONITOR":
        reasons.append(PolicyBlockReason.MONITOR_ROLE.value)
        message_fragments.append(policy_block_message(PolicyBlockReason.MONITOR_ROLE))

    if kill_switch:
        reasons.append(PolicyBlockReason.KILL_SWITCH.value)
        message_fragments.append(policy_block_message(PolicyBlockReason.KILL_SWITCH))
    if dry_run:
        reasons.append(PolicyBlockReason.DRY_RUN.value)
        message_fragments.append(policy_block_message(PolicyBlockReason.DRY_RUN))
    if not live_trading_enabled:
        reasons.append(PolicyBlockReason.NOT_ARMED.value)
    if not live_trading_ack:
        reasons.append(PolicyBlockReason.ACK_MISSING.value)
    if not live_trading_enabled or not live_trading_ack:
        reasons_message = policy_block_message(PolicyBlockReason.NOT_ARMED)
        if not message_fragments:
            message_fragments.append(reasons_message)

    allowed = not reasons
    if cycle_id and logger is not None and reasons:
        for reason in reasons:
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": decision_layer,
                    "reason_code": str(policy_reason_to_code(reason)),
                    "action": action,
                    "scope": scope,
                    "intent_id": intent_id,
                    "symbol": symbol,
                    "side": side,
                    "rule_id": str(policy_reason_to_code(reason)),
                },
            )
    if allowed:
        return LiveSideEffectsPolicyResult(
            allowed=True,
            reasons=[],
            message="Live side effects are armed",
        )

    return LiveSideEffectsPolicyResult(
        allowed=False,
        reasons=reasons,
        message=message_fragments[0] if message_fragments else "Live side effects are blocked",
    )


def policy_block_message(reason: PolicyBlockReason) -> str:
    messages = {
        PolicyBlockReason.MONITOR_ROLE: "MONITOR role blocks side effects",
        PolicyBlockReason.KILL_SWITCH: "KILL_SWITCH=true blocks side effects",
        PolicyBlockReason.DRY_RUN: "DRY_RUN=true blocks side effects",
        PolicyBlockReason.NOT_ARMED: (
            "Live trading is not armed; set LIVE_TRADING=true and LIVE_TRADING_ACK=I_UNDERSTAND"
        ),
        PolicyBlockReason.ACK_MISSING: (
            "Live trading is not armed; set LIVE_TRADING=true and LIVE_TRADING_ACK=I_UNDERSTAND"
        ),
    }
    return messages[reason]
