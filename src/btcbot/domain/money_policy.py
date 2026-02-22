from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class MoneyMathPolicy:
    """Canonical money math policy shared by execution, accounting, and ledger paths."""

    price_tick: Decimal
    qty_step: Decimal
    quote_precision: int = 8
    base_precision: int = 8
    fee_precision: int = 8
    rounding: str = ROUND_DOWN
    epsilon: Decimal = Decimal("0.00000001")


DEFAULT_MONEY_POLICY = MoneyMathPolicy(
    price_tick=Decimal("0.01"),
    qty_step=Decimal("0.00000001"),
)


def to_decimal(value: object) -> Decimal:
    """Convert supported numeric inputs to Decimal without allowing implicit float coercion."""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, float):
        raise TypeError("float values are not accepted; pass string/int/Decimal explicitly")
    raise TypeError(f"unsupported decimal conversion type: {type(value).__name__}")


def _quantize_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def _quantum(precision: int) -> Decimal:
    return Decimal("1").scaleb(-max(0, int(precision)))


def round_price(price: Decimal, policy: MoneyMathPolicy) -> Decimal:
    return _quantize_to_step(to_decimal(price), policy.price_tick, policy.rounding)


def round_qty(qty: Decimal, policy: MoneyMathPolicy) -> Decimal:
    return _quantize_to_step(to_decimal(qty), policy.qty_step, policy.rounding)


def round_fee(fee: Decimal, policy: MoneyMathPolicy) -> Decimal:
    return to_decimal(fee).quantize(_quantum(policy.fee_precision), rounding=policy.rounding)


def round_quote(amount: Decimal, policy: MoneyMathPolicy) -> Decimal:
    return to_decimal(amount).quantize(_quantum(policy.quote_precision), rounding=policy.rounding)


def policy_for_symbol(symbol_info: object) -> MoneyMathPolicy:
    """Build a canonical policy from symbol metadata (tick/step/precision)."""

    tick = getattr(symbol_info, "tick_size", None)
    if tick is None:
        tick = getattr(symbol_info, "price_tick", DEFAULT_MONEY_POLICY.price_tick)
    step = getattr(symbol_info, "lot_size", None)
    if step is None:
        step = getattr(symbol_info, "qty_step", DEFAULT_MONEY_POLICY.qty_step)

    return MoneyMathPolicy(
        price_tick=to_decimal(tick),
        qty_step=to_decimal(step),
        quote_precision=int(getattr(symbol_info, "price_precision", 8)),
        base_precision=int(getattr(symbol_info, "qty_precision", 8)),
        fee_precision=int(getattr(symbol_info, "fee_precision", 8)),
    )
