from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.services.universe_selection_service import UniverseSelectionService


@dataclass
class _Pair:
    pair_symbol: str


class _Exchange:
    def __init__(self) -> None:
        self.orderbook_calls: dict[str, int] = {}

    def get_exchange_info(self):
        return [_Pair("ETH_TRY"), _Pair("BTC_TRY"), _Pair("ADA_TRY")]

    def get_ticker_stats(self):
        return [
            {"pairSymbol": "ETH_TRY", "volume": "500", "high": "102", "low": "98", "last": "100"},
            {"pairSymbol": "BTC_TRY", "volume": "500", "high": "101", "low": "99", "last": "100"},
            {"pairSymbol": "ADA_TRY", "volume": "500", "high": "101", "low": "99", "last": "100"},
        ]

    def get_orderbook(self, symbol: str):
        self.orderbook_calls[symbol] = self.orderbook_calls.get(symbol, 0) + 1
        books = {
            "BTCTRY": (Decimal("99"), Decimal("100")),
            "ETHTRY": (Decimal("99"), Decimal("100")),
            "ADATRY": (Decimal("99"), Decimal("100")),
        }
        return books[symbol]

    def get_candles(self, symbol: str, limit: int):
        del limit
        candles = {
            "BTCTRY": [{"close": "100"}, {"close": "101"}, {"close": "99"}],
            "ETHTRY": [{"close": "100"}, {"close": "100.5"}, {"close": "100.25"}],
            "ADATRY": [{"close": "100"}, {"close": "100.5"}, {"close": "100.25"}],
        }
        return candles[symbol]


def test_deterministic_tiebreak_and_caching() -> None:
    service = UniverseSelectionService()
    exchange = _Exchange()
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)

    result = service.select_universe(
        exchange=exchange,
        settings=settings,
        now_utc=datetime.now(UTC),
    )

    assert result.selected_symbols[0] == "ADATRY"
    assert result.selected_symbols[1] == "ETHTRY"
    assert exchange.orderbook_calls == {"ADATRY": 1, "BTCTRY": 1, "ETHTRY": 1}


def test_whitelist_blacklist_behavior() -> None:
    service = UniverseSelectionService()
    exchange = _Exchange()
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_UNIVERSE_WHITELIST="BTC_TRY,ETH_TRY",
        STAGE7_UNIVERSE_BLACKLIST="ETH_TRY",
    )

    result = service.select_universe(
        exchange=exchange,
        settings=settings,
        now_utc=datetime.now(UTC),
    )

    assert result.selected_symbols == ["BTCTRY"]


def test_missing_metric_penalty_is_deterministic() -> None:
    class _MissingExchange(_Exchange):
        def get_ticker_stats(self):
            return [{"pairSymbol": "BTC_TRY", "volume": "500"}]

        def get_exchange_info(self):
            return [_Pair("BTC_TRY"), _Pair("ETH_TRY")]

        def get_orderbook(self, symbol: str):
            if symbol == "ETHTRY":
                raise ValueError("missing")
            return Decimal("99"), Decimal("100")

        def get_candles(self, symbol: str, limit: int):
            del symbol, limit
            return []

    service = UniverseSelectionService()
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    first = service.select_universe(
        exchange=_MissingExchange(),
        settings=settings,
        now_utc=datetime.now(UTC),
    )
    second = service.select_universe(
        exchange=_MissingExchange(),
        settings=settings,
        now_utc=datetime.now(UTC),
    )

    assert [x.symbol for x in first.scored] == [x.symbol for x in second.scored]
    assert first.scored[1].total_score < first.scored[0].total_score


def test_spread_bps_computation() -> None:
    spread = UniverseSelectionService.compute_spread_bps(
        best_bid=Decimal("99"),
        best_ask=Decimal("101"),
    )
    assert spread == Decimal("200")
