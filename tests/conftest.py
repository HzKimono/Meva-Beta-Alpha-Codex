from __future__ import annotations

import os
from pathlib import Path

import pytest

from btcbot.config import Settings


@pytest.fixture(autouse=True)
def isolate_settings_from_host_env(monkeypatch: pytest.MonkeyPatch):
    original_env_file = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None

    explicit = {"PYTEST_CURRENT_TEST"}
    settings_env_keys: set[str] = set()
    for field in Settings.model_fields.values():
        if isinstance(field.alias, str):
            settings_env_keys.add(field.alias)
        validation_alias = getattr(field, "validation_alias", None)
        choices = getattr(validation_alias, "choices", ())
        for choice in choices:
            if isinstance(choice, str):
                settings_env_keys.add(choice)

    for key in list(os.environ):
        if key in settings_env_keys and key not in explicit:
            monkeypatch.delenv(key, raising=False)

    for shared_db in (
        Path("/tmp/monitor_state.db"),
        Path("/tmp/health_monitor.db"),
        Path("/tmp/btcbot_state.db"),
    ):
        try:
            shared_db.unlink(missing_ok=True)
        except OSError:
            pass

    yield

    Settings.model_config["env_file"] = original_env_file


@pytest.fixture(autouse=True)
def isolate_default_state_db_per_test(
    isolate_settings_from_host_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    del isolate_settings_from_host_env
    worker_id = os.getenv("PYTEST_XDIST_WORKER", "gw0")
    test_slug = request.node.nodeid.replace(os.sep, "_").replace("/", "_").replace("::", "_")
    db_name = f"{worker_id}-{test_slug}.sqlite"
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / db_name))


@pytest.fixture
def make_live_execution_kwargs():
    def _make(**overrides):
        base = {
            "dry_run": False,
            "kill_switch": False,
            "safe_mode": False,
            "live_trading_enabled": True,
            "live_trading_ack": True,
        }
        base.update(overrides)
        return base

    return _make
