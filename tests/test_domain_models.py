from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from enum import StrEnum

import pytest

from btcbot.domain.models import (
    Order,
    OrderIntent,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    PairInfo,
    SymbolRules,
    ValidationError,
    fallback_match_by_fields,
    make_client_order_id,
    normalize_symbol,
    pair_info_to_symbol_rules,
    parse_decimal,
    quantize_price,
    quantize_quantity,
    validate_order,
)
from btcbot.domain.symbols import canonical_symbol, quote_currency, split_symbol


def test_order_enums_use_strenum() -> None:
    assert issubclass(OrderSide, StrEnum)
    assert issubclass(OrderStatus, StrEnum)


def test_order_enums_are_string_compatible() -> None:
    assert OrderSide.BUY == "buy"
    assert str(OrderStatus.CANCELED) == "canceled"


def test_order_default_timestamps_are_timezone_aware() -> None:
    order = Order(
        order_id="o-1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
    )

    assert order.created_at.tzinfo == UTC
    assert order.updated_at.tzinfo == UTC


def test_parse_decimal_comma_separator() -> None:
    assert parse_decimal("27223,7283") == Decimal("27223.7283")


def test_parse_decimal_accepts_int_float_str_none() -> None:
    assert parse_decimal(42) == Decimal("42")
    assert parse_decimal(42.5) == Decimal("42.5")
    assert parse_decimal("42.500") == Decimal("42.500")
    assert parse_decimal(None) == Decimal("0")


def test_quantize_rounds_down() -> None:
    rules = SymbolRules(pair_symbol="BTC_TRY", price_scale=2, quantity_scale=4)

    assert quantize_price(Decimal("123.459"), rules) == Decimal("123.45")
    assert quantize_quantity(Decimal("0.12349"), rules) == Decimal("0.1234")


def test_validate_order_enforces_constraints() -> None:
    rules = SymbolRules(
        pair_symbol="BTC_TRY",
        price_scale=2,
        quantity_scale=4,
        min_total=Decimal("10"),
        min_price=Decimal("1"),
        max_price=Decimal("100"),
        min_qty=Decimal("0.0010"),
        max_qty=Decimal("2.0000"),
    )

    validate_order(Decimal("10.00"), Decimal("1.0000"), rules)

    with pytest.raises(ValidationError, match="price must be > 0"):
        validate_order(Decimal("0"), Decimal("1.0000"), rules)

    with pytest.raises(ValidationError, match="quantity must be > 0"):
        validate_order(Decimal("10.00"), Decimal("0"), rules)

    with pytest.raises(ValidationError, match="price below min_price"):
        validate_order(Decimal("0.99"), Decimal("1.0000"), rules)

    with pytest.raises(ValidationError, match="price above max_price"):
        validate_order(Decimal("100.01"), Decimal("1.0000"), rules)

    with pytest.raises(ValidationError, match="quantity below min_qty"):
        validate_order(Decimal("10.00"), Decimal("0.0009"), rules)

    with pytest.raises(ValidationError, match="quantity above max_qty"):
        validate_order(Decimal("10.00"), Decimal("2.0001"), rules)

    with pytest.raises(ValidationError, match="total below min_total"):
        validate_order(Decimal("9.99"), Decimal("1.0000"), rules)

    with pytest.raises(ValidationError, match="price scale violation"):
        validate_order(Decimal("10.001"), Decimal("1.0000"), rules)

    with pytest.raises(ValidationError, match="quantity scale violation"):
        validate_order(Decimal("10.00"), Decimal("1.00001"), rules)


def test_pair_info_to_symbol_rules_extracts_scales_and_mins() -> None:
    pair = PairInfo(
        pairSymbol="BTCUSDT",
        numeratorScale=6,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        min_price=Decimal("1"),
        max_price=Decimal("100000"),
        minQuantity=Decimal("0.000001"),
        maxQuantity=Decimal("100"),
    )

    rules = pair_info_to_symbol_rules(pair)

    assert rules.pair_symbol == "BTCUSDT"
    assert rules.price_scale == 2
    assert rules.quantity_scale == 6
    assert rules.min_total == Decimal("10")
    assert rules.min_price == Decimal("1")
    assert rules.max_price == Decimal("100000")
    assert rules.min_qty == Decimal("0.000001")
    assert rules.max_qty == Decimal("100")


def test_make_client_order_id_is_deterministic() -> None:
    intent = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="cycle-abc",
    )

    first = make_client_order_id(intent)
    second = make_client_order_id(intent)

    assert first == second
    assert first.startswith("meva2-")
    assert len(first) <= 40


def test_normalize_symbol_removes_underscore_and_uppercases() -> None:
    assert normalize_symbol("btc_try") == "BTCTRY"


def test_fallback_match_allows_unknown_side_when_side_missing() -> None:
    snapshots = [
        OrderSnapshot(
            order_id="1",
            client_order_id="cid",
            pair_symbol="BTCTRY",
            side=None,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            timestamp=1700000000000,
        )
    ]
    matched = fallback_match_by_fields(
        snapshots,
        pair_symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=Decimal("100"),
        quantity=Decimal("0.1"),
        price_tolerance=Decimal("0.0001"),
        qty_tolerance=Decimal("0.0001"),
        time_window=(1699999999000, 1700000001000),
    )
    assert matched is not None


def test_canonical_symbol_normalizes_separator_and_case() -> None:
    assert canonical_symbol("btc_try") == "BTCTRY"


def test_split_symbol_supports_underscore_and_canonical_forms() -> None:
    assert split_symbol("BTC_TRY") == ("BTC", "TRY")
    assert split_symbol("btcusdt") == ("BTC", "USDT")


def test_quote_currency_supports_underscore_and_canonical_forms() -> None:
    assert quote_currency("BTC_TRY") == "TRY"
    assert quote_currency("ethusdc") == "USDC"
