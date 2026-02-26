from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.services.state_store import StateStore
from btcbot.services.universe_selection_service import UniverseSelectionService


@dataclass
class _Pair:
    pair_symbol: str
    min_total_amount: str | None = "10"
    minimum_order_amount: str | None = "0.0001"


class _Exchange:
    def __init__(self, *, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)

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
            "ETHTRY": (Decimal("99.95"), Decimal("100"), self.now),
            "ADATRY": (Decimal("99.8"), Decimal("100"), self.now),
        }
        return books[symbol]

    def get_candles(self, symbol: str, limit: int):
        del symbol, limit
        return [{"close": "100"}, {"close": "101"}, {"close": "99"}]


class _StaleAndDegradedExchange(_Exchange):
    observe_only = True

    def health_snapshot(self):
        return {"degraded": True}

    def get_orderbook_with_timestamp(self, symbol: str):
        bid, ask, _ = super().get_orderbook_with_timestamp(symbol)
        return bid, ask, self.now - timedelta(hours=1)


class _TieExchange(_Exchange):
    def get_exchange_info(self):
        return [_Pair("ZZZ_TRY"), _Pair("AAA_TRY")]

    def get_ticker_stats(self):
        return [
            {"pairSymbol": "ZZZ_TRY", "volume": "500", "high": "101", "low": "99", "last": "100"},
            {"pairSymbol": "AAA_TRY", "volume": "500", "high": "101", "low": "99", "last": "100"},
        ]

    def get_orderbook_with_timestamp(self, symbol: str):
        del symbol
        return Decimal("99.9"), Decimal("100"), self.now


class _MetadataExchange(_Exchange):
    def get_exchange_info(self):
        return [
            _Pair("GOOD_TRY", min_total_amount="10", minimum_order_amount="0.01"),
            _Pair("MISS_TRY", min_total_amount=None, minimum_order_amount=None),
            _Pair("BAD_TRY", min_total_amount="0", minimum_order_amount="0.1"),
        ]

    def get_ticker_stats(self):
        return [
            {"pairSymbol": "GOOD_TRY", "volume": "500", "high": "101", "low": "99", "last": "100"}
        ]

    def get_orderbook_with_timestamp(self, symbol: str):
        del symbol
        return Decimal("99.9"), Decimal("100"), self.now


class _GovernanceExchange(_Exchange):
    def __init__(self, now: datetime) -> None:
        super().__init__(now=now)
        self.mode = 0

    def get_exchange_info(self):
        if self.mode == 0:
            return [_Pair("AAA_TRY"), _Pair("BBB_TRY")]
        return [_Pair("AAA_TRY"), _Pair("CCC_TRY")]

    def get_ticker_stats(self):
        if self.mode == 0:
            return [
                {
                    "pairSymbol": "AAA_TRY",
                    "volume": "1000",
                    "high": "101",
                    "low": "99",
                    "last": "100",
                },
                {
                    "pairSymbol": "BBB_TRY",
                    "volume": "900",
                    "high": "101",
                    "low": "99",
                    "last": "100",
                },
            ]
        return [
            {"pairSymbol": "AAA_TRY", "volume": "1000", "high": "101", "low": "99", "last": "100"},
            {"pairSymbol": "CCC_TRY", "volume": "950", "high": "101", "low": "99", "last": "100"},
        ]

    def get_orderbook_with_timestamp(self, symbol: str):
        del symbol
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

    def get_exchange_info(self):
        if self.mode == 0:
            return [_Pair("AAA_TRY"), _Pair("BBB_TRY")]
        return [_Pair("AAA_TRY"), _Pair("CCC_TRY")]

def test_determinism_same_inputs_same_ages_scores_and_order(tmp_path) -> None:
    service = UniverseSelectionService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    settings = _settings(tmp_path)
    first = service.select_universe(exchange=_Exchange(now=now), settings=settings, now_utc=now)
    second = service.select_universe(exchange=_Exchange(now=now), settings=settings, now_utc=now)

    assert first.selected_symbols == second.selected_symbols
    assert [x.symbol for x in first.scored] == [x.symbol for x in second.scored]
    assert [x.breakdown.get("age_sec") for x in first.scored] == [
        x.breakdown.get("age_sec") for x in second.scored
    ]


def test_ranking_filters_spread_and_volume(tmp_path) -> None:
    service = UniverseSelectionService()
    result = service.select_universe(
        exchange=_Exchange(),
        settings=_settings(tmp_path),
        now_utc=datetime.now(UTC),
    )
    assert "BTCTRY" not in result.selected_symbols
    assert "ETHTRY" in result.selected_symbols


def test_freeze_reason_fidelity_preserves_multiple_reasons(tmp_path) -> None:
    service = UniverseSelectionService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    settings = _settings(tmp_path)
    first = service.select_universe(exchange=_Exchange(now=now), settings=settings, now_utc=now)
    stale = service.select_universe(
        exchange=_StaleAndDegradedExchange(now=now),
        settings=settings,
        now_utc=now,
    )
    assert stale.selected_symbols == first.selected_symbols
    assert "stale_market_data" in stale.reasons
    assert "observe_only" in stale.reasons
    assert "exchange_degraded" in stale.reasons

    store = StateStore(db_path=settings.state_db_path)
    snapshot = store.get_latest_stage7_universe_snapshot(role="stage7")
    assert snapshot is not None
    assert set(snapshot["freeze_reasons"]) == {"STALE_DATA", "OBSERVE_ONLY", "EXCHANGE_DEGRADED"}


def test_metadata_eligibility_excludes_missing_or_invalid(tmp_path) -> None:
    service = UniverseSelectionService()
    settings = _settings(tmp_path)
    _ = service.select_universe(
        exchange=_MetadataExchange(),
        settings=settings,
        now_utc=datetime.now(UTC),
    )
    store = StateStore(db_path=settings.state_db_path)
    snapshot = store.get_latest_stage7_universe_snapshot(role="STAGE7")
    assert snapshot is not None
    assert snapshot["selected_symbols"] == ["GOODTRY"]
    assert snapshot["excluded_counts"]["excluded_by_metadata_missing"] == 1
    assert snapshot["excluded_counts"]["excluded_by_min_notional"] == 1


def test_tie_break_determinism_sorts_alphabetically_on_equal_scores(tmp_path) -> None:
    service = UniverseSelectionService()
    result = service.select_universe(
        exchange=_TieExchange(),
        settings=_settings(tmp_path),
        now_utc=datetime.now(UTC),
    )
    assert [item.symbol for item in result.scored] == ["AAATRY", "ZZZTRY"]


def test_role_canonicalization_variants_resolve_single_record(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    now = datetime(2024, 1, 1, tzinfo=UTC)

    store.save_stage7_universe_snapshot(
        role="STAGE7",
        ts=now,
        selected_symbols=["BTCTRY"],
        scored=[],
        reasons=["a"],
        freeze_reason=None,
        freeze_reasons=[],
        excluded_counts={},
        churn_count=0,
    )
    store.save_stage7_universe_snapshot(
        role=" stage7 ",
        ts=now,
        selected_symbols=["ETHTRY"],
        scored=[],
        reasons=["b"],
        freeze_reason=None,
        freeze_reasons=[],
        excluded_counts={},
        churn_count=1,
    )

    a = store.get_latest_stage7_universe_snapshot(role="STAGE7")
    b = store.get_latest_stage7_universe_snapshot(role="stage7")
    c = store.get_latest_stage7_universe_snapshot(role=" Stage7 ")
    assert a is not None and b is not None and c is not None
    assert a["selected_symbols"] == b["selected_symbols"] == c["selected_symbols"] == ["ETHTRY"]


def test_governance_cooldown_and_probation(tmp_path) -> None:
    service = UniverseSelectionService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    ex = _GovernanceExchange(now=now)
    settings = _settings(
        tmp_path,
        STAGE7_UNIVERSE_GOVERNANCE_PROBATION_CYCLES=2,
        STAGE7_UNIVERSE_GOVERNANCE_COOLDOWN_SEC=7200,
    )
    first = service.select_universe(exchange=ex, settings=settings, now_utc=now)
    assert first.selected_symbols == []

    second = service.select_universe(exchange=ex, settings=settings, now_utc=now)
    assert second.selected_symbols == ["AAATRY", "BBBTRY"]

    ex.mode = 1
    third = service.select_universe(exchange=ex, settings=settings, now_utc=now)
    assert "CCCTRY" not in third.selected_symbols


def test_spread_bps_computation() -> None:
    spread = UniverseSelectionService.compute_spread_bps(
        best_bid=Decimal("99"),
        best_ask=Decimal("101"),
    )
    assert spread == Decimal("200")
