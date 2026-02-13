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
    assert first_status == "invalid"
    assert second_status == "invalid"


def test_validate_notional_price_non_positive() -> None:
    service = ExchangeRulesService(FakeExchangeClient())

    ok, reason = service.validate_notional("BTC_TRY", Decimal("0"), Decimal("1"))

    assert ok is False
    assert reason == "price_non_positive"
