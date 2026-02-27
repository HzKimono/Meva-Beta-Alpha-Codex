from __future__ import annotations

import asyncio

import pytest

from btcbot.services.rate_limiter import (
    AsyncTokenBucketRateLimiter,
    EndpointBudget,
    TokenBucketRateLimiter,
    map_endpoint_group,
)


def test_token_bucket_respects_burst_and_refill() -> None:
    now = {"t": 0.0}
    sleeps: list[float] = []

    def _clock() -> float:
        return now["t"]

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    limiter = TokenBucketRateLimiter(
        {"default": EndpointBudget(name="market_data", rps=2.0, burst=2)},
        clock=_clock,
        sleep_fn=_sleep,
    )

    assert limiter.acquire("default") == 0.0
    assert limiter.acquire("default") == 0.0
    waited = limiter.acquire("default")

    assert waited == 0.5
    assert sleeps == [0.5]


def test_penalize_on_429_applies_cooldown() -> None:
    now = {"t": 0.0}

    def _clock() -> float:
        return now["t"]

    def _sleep(seconds: float) -> None:
        now["t"] += seconds

    limiter = TokenBucketRateLimiter(
        {"default": EndpointBudget(name="market_data", rps=10.0, burst=1)},
        clock=_clock,
        sleep_fn=_sleep,
    )
    limiter.acquire("default")
    limiter.penalize_on_429("default", 2.0)
    waited = limiter.acquire("default")
    assert waited >= 2.0


def test_invalid_budget_raises_value_error() -> None:
    try:
        TokenBucketRateLimiter({"default": EndpointBudget(name="default", rps=0.0, burst=1)})
    except ValueError as exc:
        assert "rps" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_penalize_without_retry_after_applies_minimum_cooldown() -> None:
    now = {"t": 0.0}
    slept: list[float] = []

    def _clock() -> float:
        return now["t"]

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    limiter = TokenBucketRateLimiter(
        {"default": EndpointBudget(name="market_data", rps=100.0, burst=1)},
        clock=_clock,
        sleep_fn=_sleep,
    )
    limiter.acquire("default")
    limiter.penalize_on_429("default", None)
    waited = limiter.acquire("default")

    assert waited >= 0.25
    assert slept[0] >= 0.25


def test_async_token_bucket_uses_async_sleep() -> None:
    now = {"t": 0.0}
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    sync_limiter = TokenBucketRateLimiter(
        {"default": EndpointBudget(name="default", rps=1.0, burst=1)},
        clock=lambda: now["t"],
        sleep_fn=lambda _seconds: None,
    )
    limiter = AsyncTokenBucketRateLimiter(sync_limiter, sleep_fn=_sleep)

    async def _run() -> None:
        assert await limiter.acquire("orders") == 0.0
        waited = await limiter.acquire("orders")
        assert waited == 1.0

    asyncio.run(_run())
    assert slept == [1.0]


@pytest.mark.parametrize(
    ("path", "expected_group"),
    [
        ("/api/v2/orderbook?pairSymbol=BTCTRY", "market_data"),
        ("/api/v2/ticker", "market_data"),
        ("/api/v2/ohlc", "market_data"),
        ("/api/v1/order", "orders"),
        ("/api/v1/order/cancel", "orders"),
        ("/api/v1/users/balances", "account"),
        ("/api/v1/openOrders", "account"),
        ("/api/v1/users/transactions", "account"),
        ("/api/v1/unknown/path", "default"),
    ],
)
def test_map_endpoint_group_classifies_btcturk_paths(path: str, expected_group: str) -> None:
    assert map_endpoint_group(path) == expected_group


def test_rate_limiter_groups_are_isolated() -> None:
    now = {"t": 0.0}

    limiter = TokenBucketRateLimiter(
        {
            "default": EndpointBudget(name="default", rps=1.0, burst=1),
            "market_data": EndpointBudget(name="market_data", rps=1.0, burst=1),
            "orders": EndpointBudget(name="orders", rps=10.0, burst=2),
            "account": EndpointBudget(name="account", rps=2.0, burst=1),
        },
        clock=lambda: now["t"],
        sleep_fn=lambda _seconds: None,
    )

    assert limiter.consume("market_data") is True
    assert limiter.consume("market_data") is False

    assert limiter.consume("orders") is True
    assert limiter.consume("orders") is True

    limiter.penalize_on_429("market_data", 1.0)
    assert limiter.consume("market_data") is False

    now["t"] += 0.1
    assert limiter.consume("orders") is True
