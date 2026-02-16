from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic


@dataclass
class AsyncTokenBucket:
    rate_per_sec: float
    burst: int
    _tokens: float = field(init=False)
    _updated_at: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        if self.rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if self.burst < 1:
            raise ValueError("burst must be >= 1")
        self._tokens = float(self.burst)
        self._updated_at = monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate_per_sec)
        self._updated_at = now

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens <= 0:
            return
        while True:
            async with self._lock:
                now = monotonic()
                self._refill(now)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_seconds = (tokens - self._tokens) / self.rate_per_sec
            await asyncio.sleep(wait_seconds)
