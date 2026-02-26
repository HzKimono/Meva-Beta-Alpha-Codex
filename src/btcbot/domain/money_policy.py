from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from btcbot.domain.models import SymbolRules, quantize_price, quantize_quantity, validate_order


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


class OrderSizingStatus(StrEnum):
    OK = "ok"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    VIOLATES_RULES = "violates_rules"


@dataclass(frozen=True)
class OrderSizingResult:
    status: OrderSizingStatus
    quantized_price: Decimal
    quantized_quantity: Decimal
    notional_try: Decimal
    reason: str | None = None


def size_order_from_notional(
    *,
    desired_notional_try: Decimal,
    desired_price: Decimal,
    rules: SymbolRules,
    fallback_min_notional_try: Decimal,
    allow_min_notional_upgrade: bool = False,
) -> OrderSizingResult:
    """Build a deterministic order candidate from notional intent and exchange rules.

    Policy choice: strict-by-default. If desired notional is below the minimum notional,
    reject early instead of auto-upgrading size. Upgrade can be enabled explicitly via
    allow_min_notional_upgrade for controlled environments.
    """

    min_notional_try = rules.min_total or fallback_min_notional_try
    desired_notional_try = to_decimal(desired_notional_try)
    desired_price = to_decimal(desired_price)

    if desired_notional_try <= 0:
        return OrderSizingResult(
            status=OrderSizingStatus.INVALID_QUANTITY,
            quantized_price=Decimal("0"),
            quantized_quantity=Decimal("0"),
            notional_try=Decimal("0"),
            reason="desired_notional_non_positive",
        )

    if desired_price <= 0:
        return OrderSizingResult(
            status=OrderSizingStatus.INVALID_PRICE,
            quantized_price=Decimal("0"),
            quantized_quantity=Decimal("0"),
            notional_try=Decimal("0"),
            reason="desired_price_non_positive",
        )

    target_notional_try = desired_notional_try
    if min_notional_try > desired_notional_try and not allow_min_notional_upgrade:
        return OrderSizingResult(
            status=OrderSizingStatus.BELOW_MIN_NOTIONAL,
            quantized_price=quantize_price(desired_price, rules),
            quantized_quantity=Decimal("0"),
            notional_try=Decimal("0"),
            reason="desired_below_min_notional",
        )
    if allow_min_notional_upgrade:
        target_notional_try = max(target_notional_try, min_notional_try)

    quantized_price = quantize_price(desired_price, rules)
    if quantized_price <= 0:
        return OrderSizingResult(
            status=OrderSizingStatus.INVALID_PRICE,
            quantized_price=quantized_price,
            quantized_quantity=Decimal("0"),
            notional_try=Decimal("0"),
            reason="price_non_positive_after_quantize",
        )

    raw_quantity = target_notional_try / quantized_price
    quantized_quantity = quantize_quantity(raw_quantity, rules)
    notional_try = quantized_price * quantized_quantity
    if quantized_quantity <= 0:
        return OrderSizingResult(
            status=OrderSizingStatus.INVALID_QUANTITY,
            quantized_price=quantized_price,
            quantized_quantity=quantized_quantity,
            notional_try=notional_try,
            reason="quantity_non_positive_after_quantize",
        )

    if min_notional_try > 0 and notional_try < min_notional_try:
        return OrderSizingResult(
            status=OrderSizingStatus.BELOW_MIN_NOTIONAL,
            quantized_price=quantized_price,
            quantized_quantity=quantized_quantity,
            notional_try=notional_try,
            reason="notional_below_min_notional_after_quantize",
        )

    try:
        validate_order(price=quantized_price, qty=quantized_quantity, rules=rules)
    except ValueError:
        return OrderSizingResult(
            status=OrderSizingStatus.VIOLATES_RULES,
            quantized_price=quantized_price,
            quantized_quantity=quantized_quantity,
            notional_try=notional_try,
            reason="symbol_rule_validation_failed",
        )

    return OrderSizingResult(
        status=OrderSizingStatus.OK,
        quantized_price=quantized_price,
        quantized_quantity=quantized_quantity,
        notional_try=notional_try,
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
