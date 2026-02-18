from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    POLICY_BLOCK_KILL_SWITCH = "policy_block:kill_switch"
    POLICY_BLOCK_DRY_RUN = "policy_block:dry_run"
    POLICY_BLOCK_NOT_ARMED = "policy_block:not_armed"
    POLICY_BLOCK_ACK_MISSING = "policy_block:ack_missing"

    RISK_BLOCK_MAX_OPEN_ORDERS_PER_SYMBOL = "risk_block:max_open_orders_per_symbol"
    RISK_BLOCK_COOLDOWN = "risk_block:cooldown"
    RISK_BLOCK_MISSING_LIMIT_PRICE = "risk_block:missing_limit_price"
    RISK_BLOCK_MIN_NOTIONAL = "risk_block:min_notional"
    RISK_BLOCK_NON_POSITIVE_AFTER_QUANTIZE = "risk_block:non_positive_after_quantize"
    RISK_BLOCK_NOTIONAL_CAP = "risk_block:notional_cap"
    RISK_BLOCK_CASH_RESERVE_TARGET = "risk_block:cash_reserve_target"
    RISK_BLOCK_MAX_NOTIONAL_CAP_NON_POSITIVE = "risk_block:max_notional_cap_non_positive"
    RISK_CAP_MAX_NOTIONAL_PER_ORDER_TRY = "risk_cap:max_notional_per_order_try"
    RISK_DROP_MAX_ORDERS_PER_CYCLE = "risk_drop:max_orders_per_cycle"
    RISK_BLOCK_UNKNOWN = "risk_block:unknown"

    EXECUTION_SUPPRESS_SAFE_MODE = "execution_suppress:safe_mode"
    EXECUTION_SUPPRESS_KILL_SWITCH = "execution_suppress:kill_switch"
    EXECUTION_SUBMIT_DRY_RUN_SIMULATED = "execution_submit:dry_run_simulated"
    EXECUTION_REJECT_EXCHANGE_SUBMIT_FAILED = "execution_reject:exchange_submit_failed"
    EXECUTION_RECONCILE_CONFIRMED = "execution_reconcile:confirmed"
    EXECUTION_RECONCILE_UNKNOWN_BOUNDED_EXCEEDED = "execution_reconcile:unknown_bounded_exceeded"


RISK_REASON_CODE_MAP: dict[str, ReasonCode] = {
    "max_open_orders_per_symbol": ReasonCode.RISK_BLOCK_MAX_OPEN_ORDERS_PER_SYMBOL,
    "cooldown": ReasonCode.RISK_BLOCK_COOLDOWN,
    "missing_limit_price": ReasonCode.RISK_BLOCK_MISSING_LIMIT_PRICE,
    "min_notional": ReasonCode.RISK_BLOCK_MIN_NOTIONAL,
    "non_positive_after_quantize": ReasonCode.RISK_BLOCK_NON_POSITIVE_AFTER_QUANTIZE,
    "notional_cap": ReasonCode.RISK_BLOCK_NOTIONAL_CAP,
    "cash_reserve_target": ReasonCode.RISK_BLOCK_CASH_RESERVE_TARGET,
    "max_notional_per_order_cap_non_positive": ReasonCode.RISK_BLOCK_MAX_NOTIONAL_CAP_NON_POSITIVE,
}


def map_risk_reason(reason: str) -> ReasonCode:
    return RISK_REASON_CODE_MAP.get(reason, ReasonCode.RISK_BLOCK_UNKNOWN)
