from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import sleep

from btcbot.services.market_data_service import RestMarketDataProvider


class _FakeExchange:
    def __init__(self) -> None:
        self.calls = 0

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del limit
        self.calls += 1
        return (100.0 + self.calls, 101.0)


def test_cache_within_ttl_reuses_snapshot() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    exchange = _FakeExchange()
    provider = RestMarketDataProvider(exchange=exchange, now_provider=_clock, orderbook_ttl_ms=2000)

    first = provider.get_snapshot(["BTCTRY"])
    second = provider.get_snapshot(["BTCTRY"])

    assert first.bids["BTCTRY"] == second.bids["BTCTRY"]
    assert exchange.calls == 1


def test_inflight_coalescing_single_network_call() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    exchange = _FakeExchange()
    started = Event()

    original_get_orderbook = exchange.get_orderbook

    def synchronized_orderbook(symbol: str, limit: int | None = None) -> tuple[float, float]:
        started.set()
        sleep(0.05)
        return original_get_orderbook(symbol, limit)

    exchange.get_orderbook = synchronized_orderbook
    provider = RestMarketDataProvider(exchange=exchange, now_provider=_clock, orderbook_ttl_ms=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(provider.get_snapshot, ["BTCTRY"])
        assert started.wait(timeout=1)
        f2 = pool.submit(provider.get_snapshot, ["BTCTRY"])
        _ = f1.result(), f2.result()

    assert exchange.calls == 1


def test_stale_cache_can_be_used_when_fetch_fails() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    exchange = _FakeExchange()
    provider = RestMarketDataProvider(
        exchange=exchange,
        now_provider=_clock,
        orderbook_ttl_ms=50,
        orderbook_max_staleness_ms=5000,
    )
    provider.get_snapshot(["BTCTRY"])
    now = now + timedelta(milliseconds=100)
    exchange.get_orderbook = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("429"))

    snap = provider.get_snapshot(["BTCTRY"])
    assert "BTCTRY" in snap.bids


def test_stale_fallback_unblocks_waiters() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return now

    exchange = _FakeExchange()
    provider = RestMarketDataProvider(
        exchange=exchange,
        now_provider=_clock,
        orderbook_ttl_ms=10,
        orderbook_max_staleness_ms=5000,
    )
    provider.get_snapshot(["BTCTRY"])
    now = now + timedelta(milliseconds=100)

    started = Event()

    def failing_fetch(symbol: str, limit: int | None = None) -> tuple[float, float]:
        del symbol, limit
        started.set()
        sleep(0.05)
        raise RuntimeError("429")

    exchange.get_orderbook = failing_fetch

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(provider.get_snapshot, ["BTCTRY"])
        assert started.wait(timeout=1)
        b = pool.submit(provider.get_snapshot, ["BTCTRY"])
        sa = a.result(timeout=2)
        sb = b.result(timeout=2)

    assert sa.bids["BTCTRY"] == sb.bids["BTCTRY"]
