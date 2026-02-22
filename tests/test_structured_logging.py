from __future__ import annotations

import logging

from btcbot.obs.logging import cycle_context, get_logger, set_base_context
from btcbot.obs.process_role import get_process_role_from_env


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


def test_base_context_uses_app_role_env(monkeypatch, caplog) -> None:
    monkeypatch.setenv("APP_ROLE", "live")
    role = get_process_role_from_env()
    assert role.value == "LIVE"

    set_base_context(process_role=role, state_db_path="/tmp/state.db")
    logger = get_logger("btcbot.tests.obs.base")
    with caplog.at_level(logging.INFO):
        logger.info("base_context_log")

    payload = getattr(caplog.records[-1], "extra")
    assert payload["process_role"] == "LIVE"
    assert payload["state_db_path_hash"]
