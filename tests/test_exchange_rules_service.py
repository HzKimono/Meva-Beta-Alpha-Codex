from __future__ import annotations

from decimal import Decimal

import pytest

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


def test_get_rules_accepts_compact_symbol() -> None:
    exchange = FakeExchangeClient()
    service = ExchangeRulesService(exchange)

    rules = service.get_rules("BTCTRY")

    assert rules.min_notional_try == Decimal("100")
    assert rules.tick_size == Decimal("0.1")
    assert rules.step_size == Decimal("0.0001")


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


def test_get_rules_error_contains_requested_and_normalized_symbol() -> None:
    exchange = FakeExchangeClient()
    service = ExchangeRulesService(exchange)

    with pytest.raises(ValueError, match="symbol=ETH_TRY normalized=ETHTRY"):
        service.get_rules("ETH_TRY")
