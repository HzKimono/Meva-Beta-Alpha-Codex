from __future__ import annotations

import logging

from btcbot.obs.metrics import observe_histogram


def test_metric_emission_logs_record(caplog) -> None:
    with caplog.at_level(logging.INFO):
        observe_histogram(
            "bot_cycle_latency_ms",
            123,
            labels={"process_role": "MONITOR", "mode_final": "OBSERVE_ONLY"},
        )

    records = [r for r in caplog.records if r.getMessage() == "metric_emit"]
    assert records
    payload = getattr(records[-1], "extra")
    assert payload["metric_name"] == "bot_cycle_latency_ms"
