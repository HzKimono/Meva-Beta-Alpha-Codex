from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.services.decision_pipeline_service import DecisionPipelineService
from btcbot.services.dynamic_universe_service import DynamicUniverseService
from btcbot.services.state_store import StateStore


class _MockClient:
    def __init__(
        self,
        pair_symbols: list[str],
        books: dict[str, tuple[str, str, str, str]],
        *,
        ts: datetime,
        include_timestamp: bool = True,
        use_simple_orderbook: bool = False,
    ) -> None:
        self._pair_symbols = pair_symbols
        self._books = books
        self._ts = ts
        self._include_timestamp = include_timestamp
        self._use_simple_orderbook = use_simple_orderbook

    def get_exchange_info(self) -> list[PairInfo]:
        return [
            PairInfo(
                pairSymbol=symbol,
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                status="TRADING",
            )
            for symbol in self._pair_symbols
        ]

    def _get(self, path: str, params: dict[str, object]) -> dict[str, object]:
        assert path == "/api/v2/orderbook"
        symbol = str(params["pairSymbol"])
        bid_price, bid_qty, ask_price, ask_qty = self._books[symbol]
        data: dict[str, object] = {
            "bids": [[bid_price, bid_qty]],
            "asks": [[ask_price, ask_qty]],
        }
        if self._include_timestamp:
            data["timestamp"] = int(self._ts.timestamp() * 1000)
        return {"data": data}

    def get_orderbook(self, symbol: str) -> tuple[str, str]:
        bid_price, _bid_qty, ask_price, _ask_qty = self._books[symbol]
        return bid_price, ask_price


class _MockSimpleClient(_MockClient):
    def _get(self, path: str, params: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("raw unavailable")

    def get_orderbook_with_timestamp(self, symbol: str) -> tuple[str, str, int]:
        bid_price, _bid_qty, ask_price, _ask_qty = self._books[symbol]
        return bid_price, ask_price, int(self._ts.timestamp() * 1000)


class _MockExchange:
    def __init__(self, client: _MockClient) -> None:
        self.client = client




class _CountingTimestampClient(_MockClient):
    def __init__(
        self,
        pair_symbols: list[str],
        books: dict[str, tuple[str, str, str, str]],
        *,
        ts: datetime,
    ) -> None:
        super().__init__(pair_symbols, books, ts=ts, include_timestamp=True)
        self.orderbook_calls = 0

    def get_orderbook_with_timestamp(self, symbol: str) -> tuple[str, str, int]:
        self.orderbook_calls += 1
        bid_price, _bid_qty, ask_price, _ask_qty = self._books[symbol]
        return bid_price, ask_price, int(self._ts.timestamp() * 1000)




class _ExchangeTimestampOnly:
    def __init__(self, *, pair_symbols: list[str], books: dict[str, tuple[str, str, str, str]], observed_at: datetime) -> None:
        self._client = _MockClient(pair_symbols, books, ts=observed_at)
        self.client = type("NoOrderbookClient", (), {"get_exchange_info": self._client.get_exchange_info})()
        self._books = books
        self._observed_at = observed_at

    def get_exchange_info(self) -> list[PairInfo]:
        return self._client.get_exchange_info()

    def get_orderbook_with_timestamp(self, symbol: str) -> tuple[str, str, datetime]:
        bid_price, _bid_qty, ask_price, _ask_qty = self._books[symbol]
        return bid_price, ask_price, self._observed_at


def _seed_lookback(store: StateStore, now: datetime, symbols: list[str]) -> None:
    for symbol in symbols:
        store.upsert_universe_price_snapshot(
            pair_symbol=symbol,
            ts_bucket=now - timedelta(hours=24),
            mid_price=Decimal("100"),
        )


def test_freshness_stale_orderbook_rejected(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _MockExchange(
        _MockClient(
            ["AAAATRY"],
            {"AAAATRY": ("120", "500", "121", "500")},
            ts=now - timedelta(seconds=120),
        )
    )
    _seed_lookback(store, now, ["AAAATRY"])

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="[]", UNIVERSE_SPREAD_MAX_BPS=Decimal("200")),
        now_utc=now,
        cycle_id="c1",
    )

    assert result.selected_symbols == ()
    assert result.ineligible_counts["stale_orderbook"] == 1


def test_depth_unavailable_fail_closed_when_qty_missing(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _MockExchange(
        _MockSimpleClient(
            ["AAAATRY"],
            {"AAAATRY": ("120", "500", "121", "500")},
            ts=now,
        )
    )
    _seed_lookback(store, now, ["AAAATRY"])

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="[]", UNIVERSE_SPREAD_MAX_BPS=Decimal("200")),
        now_utc=now,
        cycle_id="c2",
    )

    assert result.selected_symbols == ()
    assert result.ineligible_counts["depth_unavailable"] == 1


def test_exclude_stables_and_exclude_symbols_are_independent(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    symbols = ["AAAATRY", "USDTTRY", "EURTRY"]
    exchange = _MockExchange(
        _MockClient(
            symbols,
            {
                "AAAATRY": ("120", "500", "121", "500"),
                "USDTTRY": ("40", "1000", "41", "1000"),
                "EURTRY": ("40", "1000", "41", "1000"),
            },
            ts=now,
        )
    )
    _seed_lookback(store, now, symbols)

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            UNIVERSE_EXCLUDE_STABLES=True,
            UNIVERSE_EXCLUDE_SYMBOLS='["EURTRY"]',
            UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
        ),
        now_utc=now,
        cycle_id="c3",
    )

    assert result.selected_symbols == ("AAAATRY",)
    assert result.ineligible_counts["stable_symbol"] == 1
    assert result.ineligible_counts["excluded_symbol"] == 1


def test_scoring_tie_breaks_by_symbol(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    symbols = ["BBBTRY", "AAATRY"]
    exchange = _MockExchange(
        _MockClient(
            symbols,
            {
                "AAATRY": ("100", "800", "101", "800"),
                "BBBTRY": ("100", "800", "101", "800"),
            },
            ts=now,
        )
    )
    _seed_lookback(store, now, symbols)

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="[]", UNIVERSE_TOP_N=2, UNIVERSE_SPREAD_MAX_BPS=Decimal("200")),
        now_utc=now,
        cycle_id="c4",
    )

    assert result.selected_symbols == ("AAATRY", "BBBTRY")


def test_cooldown_symbol_not_selected(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    store.upsert_dynamic_universe_symbol_state(
        symbol="AAAATRY",
        updated_at=now,
        cooldown_until_ts=now + timedelta(minutes=30),
        reject_counts={},
    )
    exchange = _MockExchange(
        _MockClient(["AAAATRY"], {"AAAATRY": ("100", "800", "101", "800")}, ts=now)
    )
    _seed_lookback(store, now, ["AAAATRY"])

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="[]", UNIVERSE_SPREAD_MAX_BPS=Decimal("200")),
        now_utc=now,
        cycle_id="c5",
    )

    assert result.selected_symbols == ()
    assert result.ineligible_counts["cooldown"] == 1




def test_probation_symbol_not_selected(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    store.upsert_dynamic_universe_symbol_state(
        symbol="AAAATRY",
        updated_at=now,
        probation_until_ts=now + timedelta(minutes=30),
        reject_counts={},
    )
    exchange = _MockExchange(
        _MockClient(["AAAATRY"], {"AAAATRY": ("100", "800", "101", "800")}, ts=now)
    )
    _seed_lookback(store, now, ["AAAATRY"])

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="[]", UNIVERSE_SPREAD_MAX_BPS=Decimal("200")),
        now_utc=now,
        cycle_id="c5b",
    )

    assert result.selected_symbols == ()
    assert result.ineligible_counts["probation"] == 1


def test_churn_guard_limits_daily_changes(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    _seed_lookback(store, now, ["AAAATRY", "BBBBTRY", "CCCCTRY"])
    svc = DynamicUniverseService()

    first = _MockExchange(
        _MockClient(
            ["AAAATRY", "BBBBTRY"],
            {
                "AAAATRY": ("110", "800", "111", "800"),
                "BBBBTRY": ("100", "800", "101", "800"),
            },
            ts=now,
        )
    )
    svc.select(
        exchange=first,
        state_store=store,
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            UNIVERSE_TOP_N=1,
            UNIVERSE_REFRESH_MINUTES=1,
            UNIVERSE_CHURN_MAX_PER_DAY=1,
            UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
        ),
        now_utc=now,
        cycle_id="c6",
    )

    second = _MockExchange(
        _MockClient(
            ["AAAATRY", "BBBBTRY"],
            {
                "AAAATRY": ("100", "800", "101", "800"),
                "BBBBTRY": ("120", "800", "121", "800"),
            },
            ts=now + timedelta(minutes=4),
        )
    )
    svc.select(
        exchange=second,
        state_store=store,
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            UNIVERSE_TOP_N=1,
            UNIVERSE_REFRESH_MINUTES=1,
            UNIVERSE_CHURN_MAX_PER_DAY=1,
            UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
        ),
        now_utc=now + timedelta(minutes=4),
        cycle_id="c7",
    )

    third = _MockExchange(
        _MockClient(
            ["AAAATRY", "BBBBTRY", "CCCCTRY"],
            {
                "AAAATRY": ("100", "800", "101", "800"),
                "BBBBTRY": ("100", "800", "101", "800"),
                "CCCCTRY": ("130", "800", "131", "800"),
            },
            ts=now + timedelta(minutes=6),
        )
    )
    result = svc.select(
        exchange=third,
        state_store=store,
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            UNIVERSE_TOP_N=1,
            UNIVERSE_REFRESH_MINUTES=1,
            UNIVERSE_CHURN_MAX_PER_DAY=1,
            UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
        ),
        now_utc=now + timedelta(minutes=6),
        cycle_id="c8",
    )

    assert result.selected_symbols == ("BBBBTRY",)
    assert result.ineligible_counts["churn_guard"] == 1


def test_aggressive_allocation_respects_cash_target_invariant() -> None:
    service = DecisionPipelineService(
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            TRY_CASH_TARGET=Decimal("300"),
            FEE_BUFFER_RATIO=Decimal("0.002"),
        ),
        now_provider=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    pair_info = [
        PairInfo(pairSymbol=f"S{i}TRY", numeratorScale=6, denominatorScale=2, minTotalAmount=10)
        for i in range(1, 6)
    ]
    scores = {f"S{i}TRY": Decimal(str(i)) for i in range(1, 6)}
    mark_prices = {f"S{i}TRY": Decimal("100") for i in range(1, 6)}

    report = service.run_cycle(
        cycle_id="cycle-3",
        balances={"TRY": Decimal("1300")},
        positions={},
        mark_prices=mark_prices,
        open_orders=[],
        pair_info=pair_info,
        bootstrap_enabled=True,
        live_mode=False,
        preferred_symbols=sorted(mark_prices.keys()),
        aggressive_scores=scores,
    )

    planned_plus_fees = report.planned_total_try * (Decimal("1") + Decimal("0.002"))
    remaining_cash = report.cash_try - planned_plus_fees
    assert report.planned_total_try > Decimal("0")
    assert remaining_cash >= Decimal("300")

def test_missing_orderbook_timestamp_falls_back_to_fetch_time(tmp_path) -> None:
    now = datetime.now(UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _MockExchange(
        _MockClient(
            ["AAAATRY"],
            {"AAAATRY": ("120", "500", "121", "500")},
            ts=now,
            include_timestamp=False,
        )
    )
    _seed_lookback(store, now, ["AAAATRY"])

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="[]", UNIVERSE_SPREAD_MAX_BPS=Decimal("200")),
        now_utc=now,
        cycle_id="cts1",
    )

    assert "orderbook_no_timestamp" not in result.ineligible_counts


def test_exchange_level_timestamped_orderbook_is_used(tmp_path) -> None:
    now = datetime.now(UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _ExchangeTimestampOnly(
        pair_symbols=["AAAATRY"],
        books={"AAAATRY": ("120", "500", "121", "500")},
        observed_at=now,
    )
    _seed_lookback(store, now, ["AAAATRY"])

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="[]", UNIVERSE_SPREAD_MAX_BPS=Decimal("200")),
        now_utc=now,
        cycle_id="cts2",
    )

    assert result.ineligible_counts.get("orderbook_unavailable", 0) == 0
    assert result.ineligible_counts.get("depth_unavailable", 0) == 1


def test_scan_budget_caps_orderbook_requests(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    symbols = ["AAAATRY", "BBBBTRY", "CCCCTRY", "DDDDTRY", "EEEETRY", "FFFFTRY"]
    books = {symbol: ("120", "500", "121", "500") for symbol in symbols}
    client = _CountingTimestampClient(symbols, books, ts=now)
    exchange = _MockExchange(client)
    _seed_lookback(store, now, symbols)

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            UNIVERSE_TOP_N=1,
            UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
            UNIVERSE_SCAN_BUDGET_SYMBOLS=4,
            UNIVERSE_MAX_ORDERBOOK_REQUESTS_PER_CYCLE=2,
        ),
        now_utc=now,
        cycle_id="budget-1",
    )

    assert client.orderbook_calls == 2
    assert result.ineligible_counts.get("scan_budget_exhausted", 0) == 4
    assert len(result.selected_symbols) <= 1



class _RaisingTimestampClient(_MockClient):
    def get_orderbook_with_timestamp(self, symbol: str) -> tuple[str, str, int]:
        raise RuntimeError(f"timestamped feed failed for {symbol}")


def test_orderbook_parse_failures_are_logged_and_counted(tmp_path, caplog) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _MockExchange(
        _RaisingTimestampClient(
            ["AAAATRY"],
            {"AAAATRY": ("120", "500", "121", "500")},
            ts=now,
        )
    )
    _seed_lookback(store, now, ["AAAATRY"])

    with caplog.at_level(logging.DEBUG):
        result = DynamicUniverseService().select(
            exchange=exchange,
            state_store=store,
            settings=Settings(
                DRY_RUN=True,
                KILL_SWITCH=False,
                SYMBOLS="[]",
                UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
            ),
            now_utc=now,
            cycle_id="diag-1",
        )

    assert result.selected_symbols == ()
    assert result.ineligible_counts["depth_unavailable"] == 1
    assert result.filters.get("diagnostics", {}).get("orderbook_parse_failure_timestamped") == 1
    assert any("dynamic_universe_orderbook_parse_failed" in rec.message for rec in caplog.records)


def test_unparseable_timestamp_is_soft_failure_with_fetch_time_fallback(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))

    class _UnparseableTimestampClient(_MockClient):
        def get_orderbook_with_timestamp(self, symbol: str) -> dict[str, object]:
            bid_price, bid_qty, ask_price, ask_qty = self._books[symbol]
            return {
                "bids": [[bid_price, bid_qty]],
                "asks": [[ask_price, ask_qty]],
                "timestamp": "not-a-real-timestamp",
            }

    exchange = _MockExchange(
        _UnparseableTimestampClient(
            ["AAAATRY"],
            {"AAAATRY": ("120", "500", "121", "500")},
            ts=now,
        )
    )
    _seed_lookback(store, now, ["AAAATRY"])

    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
            UNIVERSE_MIN_DEPTH_TRY=Decimal("1"),
        ),
        now_utc=now,
        cycle_id="parse-soft-1",
    )

    assert result.selected_symbols == ("AAAATRY",)
    diagnostics = result.filters.get("diagnostics", {})
    assert diagnostics.get("timestamp_parse_fail_count", 0) >= 1
    assert diagnostics.get("orderbook_unavailable_count") == 0
    assert diagnostics.get("depth_unavailable_count") == 0
