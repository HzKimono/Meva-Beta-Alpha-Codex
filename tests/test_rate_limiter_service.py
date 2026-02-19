from __future__ import annotations

import asyncio

from btcbot.services.rate_limiter import (
    AsyncTokenBucketRateLimiter,
    EndpointBudget,
    TokenBucketRateLimiter,
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
