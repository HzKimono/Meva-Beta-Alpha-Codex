from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum
from typing import Literal

Mode = Literal["dry_run", "live", "external"]


@dataclass(frozen=True)
class OrderId:
    value: str


@dataclass(frozen=True)
class ClientOrderId:
    value: str


class LifecycleActionType(StrEnum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    REPLACE = "replace"


class Stage4RejectReason(StrEnum):
    MIN_TOTAL = "min_total"
    MIN_QTY = "min_qty"
    TICK_SIZE = "tick_size"
    STEP_SIZE = "step_size"
    BREAKER_OPEN = "breaker_open"
    KILLSWITCH = "killswitch"
    FREEZE = "freeze"
    MISSING_RULES = "missing_rules"
    MAX_ORDER_NOTIONAL = "max_order_notional"
    UNKNOWN = "unknown"


def map_stage4_reject_reason(
    *,
    reject_code: int | str | None = None,
    reject_token: str | None = None,
) -> str:
    token = str(reject_token or "").strip().lower()
    if token in {
        "min_total",
        "min_notional_violation",
        "failed_min_total_amount",
        "prefilter_min_notional",
    }:
        return Stage4RejectReason.MIN_TOTAL.value
    if token in {"min_qty", "qty_below_min_qty_after_quantize"}:
        return Stage4RejectReason.MIN_QTY.value
    if token in {"tick_size", "price_tick_violation"}:
        return Stage4RejectReason.TICK_SIZE.value
    if token in {"step_size", "qty_step_violation"}:
        return Stage4RejectReason.STEP_SIZE.value
    if token.startswith("missing_exchange_rules"):
        return Stage4RejectReason.MISSING_RULES.value
    if token.startswith("max_order_notional"):
        return Stage4RejectReason.MAX_ORDER_NOTIONAL.value
    if token.startswith("kill_switch"):
        return Stage4RejectReason.KILLSWITCH.value
    if token.startswith("freeze"):
        return Stage4RejectReason.FREEZE.value
    if token.startswith("exchange_reject_1123"):
        return Stage4RejectReason.BREAKER_OPEN.value
    try:
        numeric_code = int(reject_code) if reject_code is not None else None
    except (TypeError, ValueError):
        numeric_code = None
    if numeric_code == 1123:
        return Stage4RejectReason.BREAKER_OPEN.value
    return Stage4RejectReason.UNKNOWN.value


@dataclass(frozen=True)
class ExchangeRules:
    tick_size: Decimal
    step_size: Decimal
    min_notional_try: Decimal
    price_precision: int
    qty_precision: int


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str
    type: str
    price: Decimal
    qty: Decimal
    status: str
    created_at: datetime
    updated_at: datetime
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    exchange_client_id: str | None = None
    mode: Mode = "dry_run"


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_asset: str
    ts: datetime


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    avg_cost_try: Decimal
    realized_pnl_try: Decimal
    last_update_ts: datetime


@dataclass(frozen=True)
class PnLSnapshot:
    total_equity_try: Decimal
    realized_today_try: Decimal
    drawdown_pct: Decimal
    ts: datetime
    realized_total_try: Decimal


@dataclass(frozen=True)
class LifecycleAction:
    action_type: LifecycleActionType
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    reason: str
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    replace_for_client_order_id: str | None = None


class Quantizer:
    @staticmethod
    def quantize_price(price: Decimal, rules: ExchangeRules) -> Decimal:
        if price <= 0:
            raise ValueError("price must be > 0")
        if rules.tick_size > 0:
            steps = (price / rules.tick_size).to_integral_value(rounding=ROUND_DOWN)
            return steps * rules.tick_size
        quantum = Decimal("1").scaleb(-rules.price_precision)
        return price.quantize(quantum, rounding=ROUND_DOWN)

    @staticmethod
    def quantize_qty(qty: Decimal, rules: ExchangeRules) -> Decimal:
        if qty <= 0:
            raise ValueError("qty must be > 0")
        if rules.step_size > 0:
            steps = (qty / rules.step_size).to_integral_value(rounding=ROUND_DOWN)
            return steps * rules.step_size
        quantum = Decimal("1").scaleb(-rules.qty_precision)
        return qty.quantize(quantum, rounding=ROUND_DOWN)

    @staticmethod
    def quantize_qty_up(qty: Decimal, rules: ExchangeRules) -> Decimal:
        if qty <= 0:
            return Decimal("0")
        if rules.step_size > 0:
            steps = (qty / rules.step_size).to_integral_value(rounding=ROUND_CEILING)
            return steps * rules.step_size
        quantum = Decimal("1").scaleb(-rules.qty_precision)
        return qty.quantize(quantum, rounding=ROUND_UP)

    @staticmethod
    def validate_min_notional(price: Decimal, qty: Decimal, rules: ExchangeRules) -> bool:
        return price * qty >= rules.min_notional_try


def now_utc() -> datetime:
    return datetime.now(UTC)
