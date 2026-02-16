from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_seconds: float


def _parse_retry_after(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (dt - datetime.now(UTC)).total_seconds())


def compute_delay(
    *,
    attempt: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    retry_after_header: str | None,
    jitter_seed: int,
) -> float:
    retry_after = _parse_retry_after(retry_after_header)
    if retry_after is not None:
        return min(max_delay_seconds, retry_after)
    prng = random.Random(jitter_seed + attempt)
    exp = min(max_delay_seconds, base_delay_seconds * (2 ** max(0, attempt - 1)))
    return exp * (0.8 + 0.4 * prng.random())


async def async_retry(  # noqa: UP047
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    classify: Callable[[Exception], RetryDecision],
) -> T:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >=1")
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            decision = classify(exc)
            if not decision.retry or attempt >= max_attempts:
                raise
            await asyncio.sleep(decision.delay_seconds)
    raise RuntimeError("retry loop exhausted")
