from __future__ import annotations

from datetime import UTC, datetime, timedelta

from btcbot.services.market_data_service import MarketDataService


class _FakeExchange:
    def __init__(self) -> None:
        self.orderbook_hits = 0

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del symbol, limit
        self.orderbook_hits += 1
        return 100.0, 101.0

    def get_exchange_info(self):
        return []


def test_market_data_deterministic_api_returns_bids_and_freshness_same_snapshot() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    service = MarketDataService(exchange=_FakeExchange(), mode="rest", now_provider=_clock)
    bids, freshness = service.get_best_bids_with_freshness(["BTC_TRY"], max_age_ms=500)

    assert bids["BTC_TRY"] == 100.0
    assert freshness.is_stale is False
    assert freshness.observed_age_ms == 0


def test_market_data_freshness_ws_disconnected_is_stale() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    service = MarketDataService(exchange=_FakeExchange(), mode="ws", now_provider=_clock)
    service.ingest_ws_best_bid("BTC_TRY", 100.0)
    service.set_ws_connected(False)

    _bids, freshness = service.get_best_bids_with_freshness(["BTC_TRY"], max_age_ms=1_000)

    assert freshness.is_stale is True
    assert freshness.connected is False


def test_market_data_freshness_ws_age_stale_without_fallback() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    service = MarketDataService(exchange=_FakeExchange(), mode="ws", now_provider=_clock)
    service.set_ws_connected(True)
    service.ingest_ws_best_bid("BTC_TRY", 100.0)
    now = now + timedelta(milliseconds=2_000)

    _bids, freshness = service.get_best_bids_with_freshness(["BTC_TRY"], max_age_ms=500)

    assert freshness.is_stale is True
    assert freshness.observed_age_ms == 2000
    assert freshness.source_mode == "ws"


def test_market_data_ws_age_stale_falls_back_to_rest_when_enabled() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    exchange = _FakeExchange()
    service = MarketDataService(
        exchange=exchange,
        mode="ws",
        ws_rest_fallback=True,
        now_provider=_clock,
    )
    service.set_ws_connected(True)
    service.ingest_ws_best_bid("BTC_TRY", 100.0)
    now = now + timedelta(milliseconds=2_000)

    bids, freshness = service.get_best_bids_with_freshness(["BTC_TRY"], max_age_ms=500)

    assert bids["BTC_TRY"] == 100.0
    assert freshness.is_stale is False
    assert freshness.source_mode == "rest_fallback"
    assert exchange.orderbook_hits == 1


def test_market_data_missing_symbols_are_reported_without_global_stale() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    service = MarketDataService(exchange=_FakeExchange(), mode="ws", now_provider=_clock)
    service.set_ws_connected(True)
    service.ingest_ws_best_bid("BTC_TRY", 100.0)

    _bids, freshness = service.get_best_bids_with_freshness(["BTC_TRY", "ETH_TRY"], max_age_ms=500)

    assert freshness.is_stale is False
    assert freshness.missing_symbols == ("ETH_TRY",)


class _CaptureInstrumentation:
    def __init__(self) -> None:
        self.counters: list[tuple[str, int]] = []

    def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
        del attrs
        self.counters.append((name, value))


def test_rest_provider_serves_usable_stale_cache_emits_degraded_counter(monkeypatch) -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    class _FlakyExchange(_FakeExchange):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False

        def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
            del limit
            if self.fail:
                raise RuntimeError("boom")
            return super().get_orderbook(symbol)

    capture = _CaptureInstrumentation()
    monkeypatch.setattr(
        "btcbot.services.market_data_service.get_instrumentation",
        lambda: capture,
    )

    exchange = _FlakyExchange()
    service = MarketDataService(
        exchange=exchange,
        mode="rest",
        now_provider=_clock,
        orderbook_ttl_ms=5,
        orderbook_max_staleness_ms=5_000,
    )

    bids, freshness = service.get_best_bids_with_freshness(["BTC_TRY"], max_age_ms=500)
    assert bids["BTC_TRY"] == 100.0
    assert freshness.is_stale is False

    now = now + timedelta(milliseconds=10)
    exchange.fail = True
    bids, freshness = service.get_best_bids_with_freshness(["BTC_TRY"], max_age_ms=500)

    assert bids["BTC_TRY"] == 100.0
    assert any(name == "market_data_degraded_total" and value == 1 for name, value in capture.counters)
