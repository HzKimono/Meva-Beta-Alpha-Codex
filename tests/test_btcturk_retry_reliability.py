from __future__ import annotations

from btcbot.adapters.btcturk.retry import compute_delay


def test_retry_after_header_wins_over_exponential_backoff() -> None:
    delay = compute_delay(
        attempt=3,
        base_delay_seconds=0.5,
        max_delay_seconds=10,
        retry_after_header="2.5",
        jitter_seed=11,
    )
    assert delay == 2.5
