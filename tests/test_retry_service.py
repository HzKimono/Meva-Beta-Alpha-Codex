from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btcbot.services.retry import parse_retry_after_seconds, retry_with_backoff


class _RateError(Exception):
    pass


def test_parse_retry_after_seconds_supports_http_date() -> None:
    dt = datetime.now(UTC) + timedelta(seconds=2)
    value = parse_retry_after_seconds(dt.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    assert value is not None
    assert 0 <= value <= 2.5


def test_retry_with_backoff_honors_max_total_sleep() -> None:
    calls = {"n": 0}

    def _fn() -> None:
        calls["n"] += 1
        raise _RateError("x")

    with pytest.raises(_RateError):
        retry_with_backoff(
            _fn,
            max_attempts=5,
            base_delay_ms=100,
            max_delay_ms=1000,
            jitter_seed=1,
            retry_on_exceptions=(_RateError,),
            sleep_fn=lambda _x: None,
            max_total_sleep_seconds=0.15,
        )
    assert calls["n"] == 2


def test_retry_after_header_takes_priority() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def _fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RateError("429")
        return "ok"

    out = retry_with_backoff(
        _fn,
        max_attempts=3,
        base_delay_ms=100,
        max_delay_ms=5000,
        jitter_seed=3,
        retry_on_exceptions=(_RateError,),
        sleep_fn=slept.append,
        retry_after_getter=lambda _exc: "2",
    )
    assert out == "ok"
    assert slept[0] >= 2.0
