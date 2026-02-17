from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PolicyBlockReason(StrEnum):
    KILL_SWITCH = "KILL_SWITCH"
    DRY_RUN = "DRY_RUN"
    NOT_ARMED = "NOT_ARMED"
    ACK_MISSING = "ACK_MISSING"


@dataclass(frozen=True)
class LiveSideEffectsPolicyResult:
    allowed: bool
    reasons: list[str]
    message: str


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
    live_trading_ack: bool,
) -> LiveSideEffectsPolicyResult:
    reasons: list[str] = []
    message_fragments: list[str] = []

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
