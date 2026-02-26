from decimal import Decimal

from btcbot.domain.models import SymbolRules
from btcbot.domain.money_policy import OrderSizingStatus, size_order_from_notional


def _rules() -> SymbolRules:
    return SymbolRules(
        pair_symbol="BTCTRY",
        price_scale=2,
        quantity_scale=4,
        min_total=Decimal("100"),
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
    )


def test_size_order_rejects_when_below_min_notional() -> None:
    result = size_order_from_notional(
        desired_notional_try=Decimal("50"),
        desired_price=Decimal("100.07"),
        rules=_rules(),
        fallback_min_notional_try=Decimal("10"),
    )

    assert result.status == OrderSizingStatus.BELOW_MIN_NOTIONAL
    assert result.reason == "desired_below_min_notional"


def test_size_order_quantizes_price_and_quantity() -> None:
    result = size_order_from_notional(
        desired_notional_try=Decimal("150"),
        desired_price=Decimal("100.07"),
        rules=_rules(),
        fallback_min_notional_try=Decimal("10"),
    )

    assert result.status == OrderSizingStatus.OK
    assert result.quantized_price == Decimal("100.0")
    assert result.quantized_quantity == Decimal("1.5")
    assert result.notional_try == Decimal("150.00")


def test_size_order_is_idempotent_for_same_inputs() -> None:
    first = size_order_from_notional(
        desired_notional_try=Decimal("150"),
        desired_price=Decimal("100.07"),
        rules=_rules(),
        fallback_min_notional_try=Decimal("10"),
    )
    second = size_order_from_notional(
        desired_notional_try=Decimal("150"),
        desired_price=Decimal("100.07"),
        rules=_rules(),
        fallback_min_notional_try=Decimal("10"),
    )

    assert first == second
