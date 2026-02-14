from __future__ import annotations

from collections.abc import Callable


class TokenBucketRateLimiter:
    def __init__(
        self,
        rate_per_sec: float,
        burst: int,
        *,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._rate_per_sec = float(rate_per_sec)
        self._capacity = float(burst)
        self._time_source = time_source or __import__("time").time
        now = self._time_source()
        self._tokens = float(burst)
        self._last_ts = now

    def _refill(self) -> None:
        now = self._time_source()
        elapsed = max(0.0, now - self._last_ts)
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + (elapsed * self._rate_per_sec))
            self._last_ts = now

    def allow(self, tokens: float = 1.0) -> bool:
        self._refill()
        return self._tokens >= tokens

    def consume(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self._tokens < tokens:
            return False
        self._tokens -= tokens
        return True

    def seconds_until_available(self, tokens: float = 1.0) -> float:
        self._refill()
        deficit = max(0.0, tokens - self._tokens)
        return deficit / self._rate_per_sec
