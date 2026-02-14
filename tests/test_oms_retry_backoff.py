from __future__ import annotations

import pytest

from btcbot.services.oms_service import NonRetryableOMSAdapterError, RateLimitError
from btcbot.services.retry import retry_with_backoff


def test_retry_transient_then_success_deterministic() -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def _fn() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise RateLimitError("retry")
        return "ok"

    value = retry_with_backoff(
        _fn,
        max_attempts=5,
        base_delay_ms=100,
        max_delay_ms=1000,
        jitter_seed=11,
        retry_on_exceptions=(RateLimitError,),
        sleep_fn=sleeps.append,
    )

    assert value == "ok"
    assert calls["count"] == 3
    assert sleeps == [0.095, 0.211]


def test_non_retryable_has_no_retries() -> None:
    calls = {"count": 0}

    def _fn() -> None:
        calls["count"] += 1
        raise NonRetryableOMSAdapterError("bad")

    with pytest.raises(NonRetryableOMSAdapterError):
        retry_with_backoff(
            _fn,
            max_attempts=5,
            base_delay_ms=100,
            max_delay_ms=1000,
            jitter_seed=7,
            retry_on_exceptions=(RateLimitError,),
        )

    assert calls["count"] == 1
