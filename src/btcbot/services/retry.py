from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, TypeVar

T = TypeVar("T")


class RetryResponseLike(Protocol):
    status_code: int
    headers: dict[str, str] | object


@dataclass(frozen=True)
class RetryAttempt:
    attempt: int
    delay_ms: int
    error_type: str


def parse_retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = float(candidate)
        return parsed if parsed >= 0 else None
    except ValueError:
        pass
    try:
        parsed_dt = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    now = datetime.now(UTC)
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=UTC)
    return max(0.0, (parsed_dt - now).total_seconds())


def retry_with_backoff(  # noqa: UP047
    fn: Callable[[], T],
    *,
    max_attempts: int,
    base_delay_ms: int,
    max_delay_ms: int,
    jitter_seed: int,
    retry_on_exceptions: Sequence[type[Exception]],
    sleep_fn: Callable[[float], None] | None = None,
    on_retry: Callable[[RetryAttempt], None] | None = None,
    retry_after_getter: Callable[[Exception], str | None] | None = None,
) -> T:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay_ms < 0 or max_delay_ms < 0:
        raise ValueError("delay values must be >= 0")

    sleep = sleep_fn or time.sleep
    retryable = tuple(retry_on_exceptions)
    prng = random.Random(jitter_seed)

    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not isinstance(exc, retryable) or attempt >= max_attempts:
                raise
            raw_delay_ms = min(max_delay_ms, base_delay_ms * (2 ** (attempt - 1)))
            delay_ms = int(raw_delay_ms * (0.5 + prng.random()))
            if retry_after_getter is not None:
                retry_after_seconds = parse_retry_after_seconds(retry_after_getter(exc))
                if retry_after_seconds is not None:
                    delay_ms = min(max_delay_ms, int(retry_after_seconds * 1000))
            if on_retry is not None:
                on_retry(
                    RetryAttempt(
                        attempt=attempt,
                        delay_ms=delay_ms,
                        error_type=type(exc).__name__,
                    )
                )
            sleep(delay_ms / 1000.0)

    raise RuntimeError("retry loop exhausted unexpectedly")
