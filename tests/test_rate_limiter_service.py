from __future__ import annotations

from btcbot.services.rate_limiter import EndpointBudget, TokenBucketRateLimiter


def test_token_bucket_respects_burst_and_refill() -> None:
    now = {"t": 0.0}
    sleeps: list[float] = []

    def _clock() -> float:
        return now["t"]

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    limiter = TokenBucketRateLimiter(
        EndpointBudget(tokens_per_second=2.0, burst_capacity=2),
        clock=_clock,
        sleep_fn=_sleep,
    )

    assert limiter.acquire("market_data") == 0.0
    assert limiter.acquire("market_data") == 0.0
    waited = limiter.acquire("market_data")

    assert waited == 0.5
    assert sleeps == [0.5]


def test_penalize_on_429_applies_cooldown() -> None:
    now = {"t": 0.0}

    def _clock() -> float:
        return now["t"]

    def _sleep(seconds: float) -> None:
        now["t"] += seconds

    limiter = TokenBucketRateLimiter(
        EndpointBudget(tokens_per_second=10.0, burst_capacity=1),
        clock=_clock,
        sleep_fn=_sleep,
    )
    limiter.acquire("market_data")
    limiter.penalize_on_429("market_data", 2.0)
    waited = limiter.acquire("market_data")
    assert waited >= 2.0


def test_invalid_budget_raises_value_error() -> None:
    try:
        TokenBucketRateLimiter(EndpointBudget(tokens_per_second=0.0, burst_capacity=1))
    except ValueError as exc:
        assert "tokens_per_second" in str(exc)
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
        EndpointBudget(tokens_per_second=100.0, burst_capacity=1),
        clock=_clock,
        sleep_fn=_sleep,
    )
    limiter.acquire("market_data")
    limiter.penalize_on_429("market_data", None)
    waited = limiter.acquire("market_data")

    assert waited >= 0.25
    assert slept[0] >= 0.25
