from __future__ import annotations

import logging

from btcbot.obs.logging import cycle_context, get_logger


def test_cycle_context_injects_fields(caplog) -> None:
    logger = get_logger("btcbot.tests.obs")
    with caplog.at_level(logging.INFO):
        with cycle_context(
            process_role="MONITOR",
            cycle_id="cycle-123",
            mode_base="DRY_RUN",
            mode_final="OBSERVE_ONLY",
        ):
            logger.info("cycle_log")

    record = caplog.records[-1]
    payload = getattr(record, "extra")
    assert payload["cycle_id"] == "cycle-123"
    assert payload["process_role"] == "MONITOR"
    assert payload["mode_base"] == "DRY_RUN"
    assert payload["mode_final"] == "OBSERVE_ONLY"
    assert payload["run_id"]
    assert payload["state_db_path_hash"]
