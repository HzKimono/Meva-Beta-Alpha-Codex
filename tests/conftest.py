from __future__ import annotations

import os

import pytest

from btcbot.config import Settings


@pytest.fixture(autouse=True)
def isolate_settings_from_host_env(monkeypatch: pytest.MonkeyPatch):
    original_env_file = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None

    prefixes = (
        "BTCTURK_",
        "STAGE7_",
        "LIVE_TRADING",
        "KILL_SWITCH",
        "DRY_RUN",
        "UNIVERSE_",
        "SYMBOLS",
    )
    explicit = {
        "LIVE_TRADING_ACK",
        "STATE_DB_PATH",
        "NOTIONAL_CAP_TRY_PER_CYCLE",
        "MAX_NOTIONAL_PER_ORDER_TRY",
        "MIN_ORDER_NOTIONAL_TRY",
        "MAX_ORDERS_PER_CYCLE",
        "TRY_CASH_TARGET",
        "DYNAMIC_UNIVERSE_ENABLED",
        "UNIVERSE_TOP_N",
        "UNIVERSE_AUTO_CORRECT",
    }
    for key in list(os.environ):
        if key.startswith(prefixes) or key in explicit:
            monkeypatch.delenv(key, raising=False)

    yield

    Settings.model_config["env_file"] = original_env_file
