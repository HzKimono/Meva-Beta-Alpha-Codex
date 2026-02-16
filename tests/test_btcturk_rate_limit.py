from __future__ import annotations

import asyncio
from time import monotonic

from btcbot.adapters.btcturk.rate_limit import AsyncTokenBucket


def test_token_bucket_throttles_when_burst_exhausted() -> None:
    bucket = AsyncTokenBucket(rate_per_sec=2.0, burst=1)

    async def _run() -> float:
        await bucket.acquire()
        start = monotonic()
        await bucket.acquire()
        return monotonic() - start

    elapsed = asyncio.run(_run())
    assert elapsed >= 0.45
