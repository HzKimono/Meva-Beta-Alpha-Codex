from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PolicyBlockReason(StrEnum):
    KILL_SWITCH = "kill_switch"
    DRY_RUN = "dry_run"
    LIVE_NOT_ARMED = "live_not_armed"


@dataclass(frozen=True)
class LiveSideEffectsPolicyResult:
    allowed: bool
    reasons: list[str]
    message: str


def validate_live_side_effects_policy(
    *,
    dry_run: bool,
    kill_switch: bool,
    live_trading_enabled: bool,
    live_trading_ack: bool = True,
) -> LiveSideEffectsPolicyResult:
    reasons: list[str] = []
    message_fragments: list[str] = []

    if kill_switch:
        reasons.append("KILL_SWITCH")
        message_fragments.append(policy_block_message(PolicyBlockReason.KILL_SWITCH))
    if dry_run:
        reasons.append("DRY_RUN")
        message_fragments.append(policy_block_message(PolicyBlockReason.DRY_RUN))
    if not live_trading_enabled:
        reasons.append("NOT_ARMED")
    if not live_trading_ack:
        reasons.append("ACK_MISSING")
    if not live_trading_enabled or not live_trading_ack:
        reasons_message = policy_block_message(PolicyBlockReason.LIVE_NOT_ARMED)
        if not message_fragments:
            message_fragments.append(reasons_message)

    allowed = not reasons
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
    if reason is PolicyBlockReason.KILL_SWITCH:
        return "KILL_SWITCH=true blocks side effects"
    if reason is PolicyBlockReason.DRY_RUN:
        return "DRY_RUN=true blocks side effects"
    return "Live trading is not armed; set LIVE_TRADING=true and LIVE_TRADING_ACK=I_UNDERSTAND"
