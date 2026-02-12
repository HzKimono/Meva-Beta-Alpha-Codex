from __future__ import annotations

from decimal import Decimal

import httpx

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.domain.models import pair_info_to_symbol_rules, quantize_price, quantize_quantity


def test_to_pair_info_parses_real_btcturk_shape() -> None:
    client = BtcturkHttpClient()

    pair = client._to_pair_info(
        {
            "name": "BTCTRY",
            "nameNormalized": "BTC_TRY",
            "numerator": "BTC",
            "denominator": "TRY",
            "numeratorScale": 8,
            "denominatorScale": 2,
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": "10",
                    "minExchangeValue": "99.91",
                    "minPrice": "10",
                    "maxPrice": "9999999",
                }
            ],
        }
    )

    assert pair.pair_symbol == "BTC_TRY"
    assert pair.min_total_amount == Decimal("99.91")
    assert pair.min_price == Decimal("10")
    assert pair.max_price == Decimal("9999999")


def test_to_pair_info_supports_pair_symbol_variant() -> None:
    client = BtcturkHttpClient()

    pair = client._to_pair_info(
        {
            "pairSymbol": "BTCTRY",
            "numerator": "BTC",
            "denominator": "TRY",
            "numeratorScale": 8,
            "denominatorScale": 2,
        }
    )

    assert pair.pair_symbol == "BTC_TRY"


def test_to_pair_info_optional_missing_fields_do_not_raise() -> None:
    client = BtcturkHttpClient()

    pair = client._to_pair_info(
        {
            "name": "SOLTRY",
            "numerator": "SOL",
            "denominator": "TRY",
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": None}],
        }
    )

    assert pair.pair_symbol == "SOL_TRY"
    assert pair.min_total_amount is None


def test_get_exchange_info_skips_malformed_rows_when_some_are_valid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/server/exchangeinfo":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "symbols": [
                            {"pairSymbol": "BTCTRY", "numerator": "BTC", "denominator": "TRY"},
                            {"status": "TRADING"},
                        ]
                    },
                },
            )
        return httpx.Response(404)

    client = BtcturkHttpClient(transport=httpx.MockTransport(handler))

    pairs = client.get_exchange_info()

    assert len(pairs) == 1
    assert pairs[0].pair_symbol == "BTC_TRY"
    client.close()


def test_symbol_rules_use_tick_and_step_size_when_present() -> None:
    client = BtcturkHttpClient()
    pair = client._to_pair_info(
        {
            "pairSymbol": "BTCTRY",
            "numerator": "BTC",
            "denominator": "TRY",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "10"},
                {"filterType": "QUANTITY_FILTER", "stepSize": "0.0001"},
            ],
        }
    )

    rules = pair_info_to_symbol_rules(pair)

    assert quantize_price(Decimal("123.99"), rules) == Decimal("120")
    assert quantize_quantity(Decimal("0.12349"), rules) == Decimal("0.1234")
