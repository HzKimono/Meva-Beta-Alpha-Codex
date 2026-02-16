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


def parse_retry_after_seconds(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    try:
        seconds = float(candidate)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def compute_delay(
    *,
    attempt: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    retry_after_header: str | None,
    jitter_seed: int,
) -> float:
    retry_after_seconds = parse_retry_after_seconds(retry_after_header)
    if retry_after_seconds is not None:
        return min(max_delay_seconds, retry_after_seconds)

    bounded_attempt = max(1, attempt)
    exp_delay = min(max_delay_seconds, base_delay_seconds * (2 ** (bounded_attempt - 1)))
    rng = random.Random(jitter_seed + bounded_attempt)
    return exp_delay * (0.8 + (0.4 * rng.random()))


async def async_retry(  # noqa: UP047
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    classify: Callable[[Exception, int], RetryDecision],
) -> T:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            decision = classify(exc, attempt)
            if not decision.retry or attempt >= max_attempts:
                raise
            await asyncio.sleep(max(0.0, decision.delay_seconds))

    raise RuntimeError("Retry loop exhausted unexpectedly")
