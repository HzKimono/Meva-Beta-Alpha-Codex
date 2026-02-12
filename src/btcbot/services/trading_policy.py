from __future__ import annotations

from enum import StrEnum


class PolicyBlockReason(StrEnum):
    KILL_SWITCH = "kill_switch"
    DRY_RUN = "dry_run"
    LIVE_NOT_ARMED = "live_not_armed"


def validate_live_side_effects_policy(
    *,
    dry_run: bool,
    kill_switch: bool,
    live_trading_enabled: bool,
) -> PolicyBlockReason | None:
    if kill_switch:
        return PolicyBlockReason.KILL_SWITCH
    if dry_run:
        return PolicyBlockReason.DRY_RUN
    if not live_trading_enabled:
        return PolicyBlockReason.LIVE_NOT_ARMED
    return None


def policy_block_message(reason: PolicyBlockReason) -> str:
    if reason is PolicyBlockReason.KILL_SWITCH:
        return "KILL_SWITCH=true blocks side effects"
    if reason is PolicyBlockReason.DRY_RUN:
        return "DRY_RUN=true blocks side effects"
    return "Live trading is not armed; set LIVE_TRADING=true and LIVE_TRADING_ACK=I_UNDERSTAND"
