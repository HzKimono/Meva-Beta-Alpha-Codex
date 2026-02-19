from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.domain.models import (
    SymbolRules,
    ValidationError,
    quantize_price,
    quantize_quantity,
    validate_order,
)
from btcbot.services.market_data_service import MarketDataService, SymbolRulesNotFoundError


def _exchangeinfo_payload() -> dict:
    return {
        "success": True,
        "data": {
            "symbols": [
                {
                    "pairSymbol": "BTCTRY",
                    "name": "BTC/TRY",
                    "nameNormalized": "BTC_TRY",
                    "status": "TRADING",
                    "numerator": "BTC",
                    "denominator": "TRY",
                    "numeratorScale": 8,
                    "denominatorScale": 2,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "minPrice": "10", "maxPrice": "9999999"},
                        {
                            "filterType": "QUANTITY_FILTER",
                            "minQuantity": "0.00001",
                            "maxQuantity": "100",
                        },
                        {"filterType": "MIN_TOTAL", "minTotalAmount": "100"},
                    ],
                },
                {
                    "pairSymbol": "ETHTRY",
                    "name": "ETH/TRY",
                    "nameNormalized": "ETH_TRY",
                    "status": "TRADING",
                    "numerator": "ETH",
                    "denominator": "TRY",
                    "numeratorScale": 6,
                    "denominatorScale": 2,
                    "minTotalAmount": "50",
                    "minPrice": "1",
                    "maxPrice": "1000000",
                    "minQuantity": "0.001",
                    "maxQuantity": "500",
                },
            ]
        },
    }


def test_exchange_info_parsing_extracts_pair_rules() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/server/exchangeinfo":
            return httpx.Response(200, json=_exchangeinfo_payload())
        return httpx.Response(404)

    client = BtcturkHttpClient(
        transport=httpx.MockTransport(handler), base_url="https://api.btcturk.com"
    )

    pairs = client.get_exchange_info()

    assert len(pairs) == 2
    assert pairs[0].pair_symbol == "BTCTRY"
    assert pairs[0].denominator_scale == 2
    assert pairs[0].min_total_amount == Decimal("100")
    assert pairs[0].min_quantity == Decimal("0.00001")
    assert pairs[1].pair_symbol == "ETHTRY"
    client.close()


def test_quantize_helpers_round_down() -> None:
    rules = SymbolRules(pair_symbol="BTCTRY", price_scale=2, quantity_scale=5)

    assert quantize_price(Decimal("123.456"), rules) == Decimal("123.45")
    assert quantize_quantity(Decimal("0.1234567"), rules) == Decimal("0.12345")


def test_validate_order_price_scale_violation() -> None:
    rules = SymbolRules(pair_symbol="BTCTRY", price_scale=2, quantity_scale=5)

    with pytest.raises(ValidationError, match="price scale violation"):
        validate_order(Decimal("10.001"), Decimal("1"), rules)


def test_validate_order_min_qty_violation() -> None:
    rules = SymbolRules(
        pair_symbol="BTCTRY",
        price_scale=2,
        quantity_scale=5,
        min_qty=Decimal("0.01"),
    )

    with pytest.raises(ValidationError, match="quantity below min_qty"):
        validate_order(Decimal("10.00"), Decimal("0.00100"), rules)


def test_validate_order_min_total_violation() -> None:
    rules = SymbolRules(
        pair_symbol="BTCTRY",
        price_scale=2,
        quantity_scale=5,
        min_total=Decimal("1"),
        min_qty=Decimal("0.00001"),
    )

    with pytest.raises(ValidationError, match="total below min_total"):
        validate_order(Decimal("10.00"), Decimal("0.00100"), rules)


def test_market_data_service_rules_cache_calls_once() -> None:
    hit_count = {"exchangeinfo": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/server/exchangeinfo":
            hit_count["exchangeinfo"] += 1
            return httpx.Response(200, json=_exchangeinfo_payload())
        return httpx.Response(404)

    client = BtcturkHttpClient(
        transport=httpx.MockTransport(handler), base_url="https://api.btcturk.com"
    )
    service = MarketDataService(exchange=client)

    first = service.get_symbol_rules("BTCTRY")
    second = service.get_symbol_rules("BTCTRY")

    assert first.pair_symbol == "BTCTRY"
    assert second.pair_symbol == "BTCTRY"
    assert hit_count["exchangeinfo"] == 1
    client.close()


def test_market_data_service_unknown_pair_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/server/exchangeinfo":
            return httpx.Response(200, json=_exchangeinfo_payload())
        return httpx.Response(404)

    client = BtcturkHttpClient(
        transport=httpx.MockTransport(handler), base_url="https://api.btcturk.com"
    )
    service = MarketDataService(exchange=client)

    with pytest.raises(SymbolRulesNotFoundError, match="Unknown symbol rules"):
        service.get_symbol_rules("DOESTRY")
    client.close()


def test_market_data_service_rules_lookup_accepts_canonical_and_underscore() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/server/exchangeinfo":
            return httpx.Response(200, json=_exchangeinfo_payload())
        return httpx.Response(404)

    client = BtcturkHttpClient(
        transport=httpx.MockTransport(handler), base_url="https://api.btcturk.com"
    )
    service = MarketDataService(exchange=client)

    canonical = service.get_symbol_rules("BTCTRY")
    underscore = service.get_symbol_rules("BTC_TRY")

    assert canonical == underscore
    assert canonical.pair_symbol == "BTCTRY"
    client.close()
