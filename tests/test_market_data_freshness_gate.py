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


def test_market_data_freshness_rest_age_computed_from_fetch_time() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    service = MarketDataService(exchange=_FakeExchange(), mode="rest", now_provider=_clock)
    service.get_best_bids(["BTC_TRY"])

    now = now + timedelta(milliseconds=250)
    freshness = service.get_market_data_freshness(max_age_ms=500)

    assert freshness.is_stale is False
    assert freshness.observed_age_ms == 250


def test_market_data_freshness_ws_disconnected_is_stale() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    service = MarketDataService(exchange=_FakeExchange(), mode="ws", now_provider=_clock)
    service.ingest_ws_best_bid("BTC_TRY", 100.0)
    service.set_ws_connected(False)
    service.get_best_bids(["BTC_TRY"])

    freshness = service.get_market_data_freshness(max_age_ms=1_000)

    assert freshness.is_stale is True
    assert freshness.connected is False


def test_market_data_freshness_ws_age_stale_when_too_old() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    service = MarketDataService(exchange=_FakeExchange(), mode="ws", now_provider=_clock)
    service.set_ws_connected(True)
    service.ingest_ws_best_bid("BTC_TRY", 100.0)
    service.get_best_bids(["BTC_TRY"])

    now = now + timedelta(milliseconds=2_000)
    freshness = service.get_market_data_freshness(max_age_ms=500)

    assert freshness.is_stale is True
    assert freshness.observed_age_ms == 2000
