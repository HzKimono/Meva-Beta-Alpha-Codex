from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.services.universe_selection_service import UniverseSelectionService


@dataclass
class _Pair:
    pair_symbol: str


class _Exchange:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def get_exchange_info(self):
        return [_Pair("ETH_TRY"), _Pair("BTC_TRY"), _Pair("ADA_USDT"), _Pair("ADA_TRY")]

    def get_ticker_stats(self):
        return [
            {"pairSymbol": "ETH_TRY", "volume": "500", "high": "102", "low": "98", "last": "100"},
            {"pairSymbol": "BTC_TRY", "volume": "50", "high": "101", "low": "99", "last": "100"},
            {"pairSymbol": "ADA_TRY", "volume": "400", "high": "101", "low": "99", "last": "100"},
        ]

    def get_orderbook_with_timestamp(self, symbol: str):
        books = {
            "BTCTRY": (Decimal("99"), Decimal("101"), self.now),
            "ETHTRY": (Decimal("99.9"), Decimal("100"), self.now),
            "ADATRY": (Decimal("99.8"), Decimal("100"), self.now),
        }
        return books[symbol]

    def get_candles(self, symbol: str, limit: int):
        del symbol, limit
        return [{"close": "100"}, {"close": "101"}, {"close": "99"}]


class _StaleExchange(_Exchange):
    def get_orderbook_with_timestamp(self, symbol: str):
        bid, ask, _ = super().get_orderbook_with_timestamp(symbol)
        return bid, ask, self.now - timedelta(hours=1)


class _GovernanceExchange(_Exchange):
    def __init__(self) -> None:
        super().__init__()
        self.mode = 0

    def get_exchange_info(self):
        if self.mode == 0:
            return [_Pair("AAA_TRY"), _Pair("BBB_TRY")]
        return [_Pair("AAA_TRY"), _Pair("CCC_TRY")]

    def get_ticker_stats(self):
        if self.mode == 0:
            return [
                {"pairSymbol": "AAA_TRY", "volume": "1000", "high": "101", "low": "99", "last": "100"},
                {"pairSymbol": "BBB_TRY", "volume": "900", "high": "101", "low": "99", "last": "100"},
            ]
        return [
            {"pairSymbol": "AAA_TRY", "volume": "1000", "high": "101", "low": "99", "last": "100"},
            {"pairSymbol": "CCC_TRY", "volume": "950", "high": "101", "low": "99", "last": "100"},
        ]

    def get_orderbook_with_timestamp(self, symbol: str):
        return Decimal("99.9"), Decimal("100"), self.now


def _settings(tmp_path, **extra):
    env = {
        "DRY_RUN": True,
        "STAGE7_ENABLED": True,
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "STAGE7_UNIVERSE_SIZE": 2,
        "STAGE7_MIN_QUOTE_VOLUME_TRY": "100",
        "STAGE7_MAX_SPREAD_BPS": "50",
        "STAGE7_MAX_DATA_AGE_SEC": 30,
        "STAGE7_UNIVERSE_GOVERNANCE_PROBATION_CYCLES": 1,
        "STAGE7_UNIVERSE_GOVERNANCE_MAX_CHURN_PER_DAY": 100,
        "STAGE7_UNIVERSE_GOVERNANCE_COOLDOWN_SEC": 3600,
    }
    env.update(extra)
    return Settings(**env)


def test_discovery_try_only_and_normalized(tmp_path) -> None:
    service = UniverseSelectionService()
    result = service.select_universe(
        exchange=_Exchange(),
        settings=_settings(tmp_path),
        now_utc=datetime.now(UTC),
    )
    assert all(symbol.endswith("TRY") for symbol in result.selected_symbols)
    assert "ADAUSDT" not in result.selected_symbols


def test_ranking_filters_spread_and_volume(tmp_path) -> None:
    service = UniverseSelectionService()
    result = service.select_universe(
        exchange=_Exchange(),
        settings=_settings(tmp_path),
        now_utc=datetime.now(UTC),
    )
    assert "BTCTRY" not in result.selected_symbols
    assert "ETHTRY" in result.selected_symbols


def test_freeze_on_stale_returns_previous_universe(tmp_path) -> None:
    service = UniverseSelectionService()
    fresh = service.select_universe(
        exchange=_Exchange(),
        settings=_settings(tmp_path),
        now_utc=datetime.now(UTC),
    )
    stale = service.select_universe(
        exchange=_StaleExchange(),
        settings=_settings(tmp_path),
        now_utc=datetime.now(UTC),
    )
    assert stale.selected_symbols == fresh.selected_symbols
    assert "stale_market_data" in stale.reasons


def test_governance_cooldown_and_probation(tmp_path) -> None:
    service = UniverseSelectionService()
    ex = _GovernanceExchange()
    settings = _settings(
        tmp_path,
        STAGE7_UNIVERSE_GOVERNANCE_PROBATION_CYCLES=2,
        STAGE7_UNIVERSE_GOVERNANCE_COOLDOWN_SEC=7200,
    )
    first = service.select_universe(exchange=ex, settings=settings, now_utc=datetime.now(UTC))
    assert first.selected_symbols == []
    second = service.select_universe(exchange=ex, settings=settings, now_utc=datetime.now(UTC))
    assert second.selected_symbols == ["AAATRY", "BBBTRY"]
    ex.mode = 1
    third = service.select_universe(exchange=ex, settings=settings, now_utc=datetime.now(UTC))
    assert "CCC" not in "".join(third.selected_symbols)


def test_determinism_same_input_same_order(tmp_path) -> None:
    service = UniverseSelectionService()
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    first = service.select_universe(exchange=_Exchange(), settings=settings, now_utc=now)
    second = service.select_universe(exchange=_Exchange(), settings=settings, now_utc=now)
    assert first.selected_symbols == second.selected_symbols
    assert [x.symbol for x in first.scored] == [x.symbol for x in second.scored]


def test_spread_bps_computation() -> None:
    spread = UniverseSelectionService.compute_spread_bps(
        best_bid=Decimal("99"),
        best_ask=Decimal("101"),
    )
    assert spread == Decimal("200")
