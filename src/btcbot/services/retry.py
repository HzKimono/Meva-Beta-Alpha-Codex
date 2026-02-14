from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryAttempt:
    attempt: int
    delay_ms: int
    error_type: str


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
) -> T:
    """Run `fn` with deterministic exponential backoff on retryable exceptions."""
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
            jitter_multiplier = 0.5 + prng.random()
            delay_ms = int(raw_delay_ms * jitter_multiplier)
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
