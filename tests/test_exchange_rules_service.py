from __future__ import annotations

from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.services.exchange_rules_service import ExchangeRulesService


class FakeExchangeClient:
    def __init__(self) -> None:
        self.exchange_info_calls = 0

    def get_exchange_info(self) -> list[PairInfo]:
        self.exchange_info_calls += 1
        return [
            PairInfo(
                pairSymbol="BTC_TRY",
                name="BTCTRY",
                nameNormalized="BTC_TRY",
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("100"),
                tickSize=Decimal("0.1"),
                stepSize=Decimal("0.0001"),
            )
        ]


class _InvalidPair:
    pair_symbol = "BTC_TRY"
    name = "BTCTRY"
    tick_size = Decimal("0")
    step_size = Decimal("0.0001")
    min_total_amount = Decimal("100")


class InvalidMetadataExchangeClient:
    def get_exchange_info(self):
        return [_InvalidPair()]


def test_get_rules_accepts_compact_symbol() -> None:
    exchange = FakeExchangeClient()
    service = ExchangeRulesService(exchange)

    rules = service.get_rules("BTCTRY")

    assert rules.min_notional_try == Decimal("100")
    assert rules.tick_size == Decimal("0.1")
    assert rules.lot_size == Decimal("0.0001")


def test_get_rules_accepts_underscore_symbol() -> None:
    exchange = FakeExchangeClient()
    service = ExchangeRulesService(exchange)

    rules = service.get_rules("BTC_TRY")

    assert rules.min_notional_try == Decimal("100")


def test_get_rules_accepts_dash_symbol() -> None:
    exchange = FakeExchangeClient()
    service = ExchangeRulesService(exchange)

    rules = service.get_rules("BTC-TRY")

    assert rules.min_notional_try == Decimal("100")


def test_rules_cache_hit_across_aliases() -> None:
    exchange = FakeExchangeClient()
    service = ExchangeRulesService(exchange)

    first = service.get_rules("BTC_TRY")
    second = service.get_rules("BTCTRY")
    third = service.get_rules("BTC-TRY")

    assert first == second == third
    assert exchange.exchange_info_calls == 1


def test_rules_require_metadata_true_disables_fallback() -> None:
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_RULES_REQUIRE_METADATA=True,
    )
    service = ExchangeRulesService(FakeExchangeClient(), settings=settings)

    rules, status = service.get_symbol_rules_status("ETH_TRY")

    assert rules is None
    assert status == "missing"


def test_invalid_metadata_not_cached_as_zero() -> None:
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_RULES_REQUIRE_METADATA=True,
    )
    service = ExchangeRulesService(InvalidMetadataExchangeClient(), settings=settings)

    first_rules, first_status = service.get_symbol_rules_status("BTC_TRY")
    second_rules, second_status = service.get_symbol_rules_status("BTC_TRY")

    assert first_rules is None
    assert second_rules is None
    assert first_status == "invalid_metadata"
    assert second_status == "invalid_metadata"


def test_fallback_cache_preserves_status_when_metadata_not_required() -> None:
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_RULES_REQUIRE_METADATA=False,
    )
    exchange = FakeExchangeClient()
    service = ExchangeRulesService(exchange, settings=settings)

    first_rules, first_status = service.get_symbol_rules_status("ETH_TRY")
    second_rules, second_status = service.get_symbol_rules_status("ETH_TRY")

    assert first_rules is not None
    assert second_rules is not None
    assert first_status == "fallback"
    assert second_status == "fallback"
    assert exchange.exchange_info_calls == 1


def test_validate_notional_price_non_positive() -> None:
    service = ExchangeRulesService(FakeExchangeClient())

    ok, reason = service.validate_notional("BTC_TRY", Decimal("0"), Decimal("1"))

    assert ok is False
    assert reason == "price_non_positive"


class DictFiltersExchangeClient:
    def __init__(self) -> None:
        self.exchange_info_calls = 0

    def get_exchange_info(self) -> list[dict[str, object]]:
        self.exchange_info_calls += 1
        return [
            {
                "name": "BTCTRY",
                "nameNormalized": "BTC_TRY",
                "status": "TRADING",
                "numeratorScale": 8,
                "denominatorScale": 0,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": "1",
                        "minExchangeValue": "99.91",
                    }
                ],
            },
            {
                "name": "ETHTRY",
                "nameNormalized": "ETH_TRY",
                "status": "TRADING",
                "numeratorScale": 6,
                "denominatorScale": 2,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": "0.1",
                        "minExchangeValue": "99.91",
                    }
                ],
            },
        ]


def test_dict_payload_filters_are_parsed_and_lot_size_derived() -> None:
    service = ExchangeRulesService(DictFiltersExchangeClient())

    rules, status = service.get_symbol_rules_status("ETHTRY")

    assert status == "ok"
    assert rules is not None
    assert rules.tick_size == Decimal("0.1")
    assert rules.min_notional_try == Decimal("99.91")
    assert rules.lot_size == Decimal("0.000001")
    assert rules.price_precision == 2
    assert rules.qty_precision == 6


def test_dict_payload_aliasing_uses_same_cached_rules() -> None:
    exchange = DictFiltersExchangeClient()
    service = ExchangeRulesService(exchange)

    a = service.get_rules("BTC_TRY")
    b = service.get_rules("BTCTRY")

    assert a == b
    assert exchange.exchange_info_calls == 1


class ExchangeInfoErrorClient:
    def get_exchange_info(self):
        raise TimeoutError("boom")


def test_resolve_symbol_rules_returns_error_without_throwing() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_RULES_REQUIRE_METADATA=True)
    service = ExchangeRulesService(ExchangeInfoErrorClient(), settings=settings)

    resolution = service.resolve_symbol_rules("BTC_TRY")

    assert resolution.usable is False
    assert resolution.status == "upstream_fetch_failure"
    assert resolution.reason == "upstream_fetch_failure:TimeoutError"


def test_rules_parser_supports_min_notional_filter_variants() -> None:
    class MinNotionalClient:
        def get_exchange_info(self):
            return [
                {
                    "pairSymbol": "BTCTRY",
                    "numeratorScale": "8",
                    "denominatorScale": "2",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "250"},
                    ],
                }
            ]

    service = ExchangeRulesService(MinNotionalClient())
    rules, status = service.get_symbol_rules_status("BTC_TRY")

    assert status == "ok"
    assert rules is not None
    assert rules.min_notional_try == Decimal("250")


class FlakyExchangeInfoClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_exchange_info(self):
        self.calls += 1
        if self.calls % 2 == 1:
            raise ConnectionError("temporary")
        return [
            {
                "pairSymbol": "BTCTRY",
                "numeratorScale": 8,
                "denominatorScale": 2,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": "0.1",
                        "minExchangeValue": "100",
                    }
                ],
            }
        ]


def test_flaky_exchangeinfo_never_raises_from_resolve() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_RULES_REQUIRE_METADATA=True)
    service = ExchangeRulesService(FlakyExchangeInfoClient(), settings=settings)

    first = service.resolve_symbol_rules("BTC_TRY")
    second = service.resolve_symbol_rules("BTC_TRY")

    assert first.status == "upstream_fetch_failure"
    assert first.rules is None
    assert second.status == "ok"
    assert second.rules is not None


class UnsupportedVariantClient:
    def get_exchange_info(self):
        return [
            {
                "pairSymbol": "BTCTRY",
                "filters": {"price": {"tickSize": "0.1"}},
            }
        ]


def test_rules_boundary_decision_uses_typed_outcomes() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_RULES_REQUIRE_METADATA=True)
    service = ExchangeRulesService(UnsupportedVariantClient(), settings=settings)

    decision = service.resolve_boundary("BTC_TRY")

    assert decision.outcome == "SKIP"
    assert decision.resolution.status == "invalid_metadata"
    assert decision.rules is None


def test_invalid_metadata_reason_lists_fields() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_RULES_REQUIRE_METADATA=True)
    service = ExchangeRulesService(InvalidMetadataExchangeClient(), settings=settings)

    resolution = service.resolve_symbol_rules("BTC_TRY")

    assert resolution.status == "invalid_metadata"
    assert resolution.reason is not None
    assert "invalid=tick_size" in resolution.reason
