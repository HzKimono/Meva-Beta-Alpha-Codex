from __future__ import annotations

from datetime import UTC, datetime, timedelta

from btcbot.adapters.btcturk.retry import compute_delay, parse_retry_after_seconds


def test_retry_after_header_wins_over_exponential_backoff() -> None:
    delay = compute_delay(
        attempt=3,
        base_delay_seconds=0.5,
        max_delay_seconds=10,
        retry_after_header="2.5",
        jitter_seed=11,
    )
    assert delay == 2.5


def test_retry_after_http_date_supported() -> None:
    at = datetime.now(UTC) + timedelta(seconds=3)
    parsed = parse_retry_after_seconds(at.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    assert parsed is not None
    assert 0 <= parsed <= 3.5


def test_backoff_uses_attempt_progression() -> None:
    d1 = compute_delay(
        attempt=1,
        base_delay_seconds=0.2,
        max_delay_seconds=10,
        retry_after_header=None,
        jitter_seed=1,
    )
    d3 = compute_delay(
        attempt=3,
        base_delay_seconds=0.2,
        max_delay_seconds=10,
        retry_after_header=None,
        jitter_seed=1,
    )
    assert d3 > d1
