from __future__ import annotations

from pathlib import Path

import pytest

from btcbot.runtime.guards import (
    enforce_role_db_convention,
    normalize_db_path,
    require_no_dotenv,
)


def test_require_no_dotenv_fails_with_env_file_arg() -> None:
    with pytest.raises(ValueError, match="Dotenv bootstrap is disabled"):
        require_no_dotenv(".env.live")


def test_require_no_dotenv_fails_with_settings_env_file(monkeypatch) -> None:
    monkeypatch.setenv("SETTINGS_ENV_FILE", ".env.live")

    with pytest.raises(ValueError, match="Dotenv bootstrap is disabled"):
        require_no_dotenv(None)


def test_normalize_db_path_fails_when_empty() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        normalize_db_path("")


def test_normalize_db_path_fails_when_relative() -> None:
    with pytest.raises(ValueError, match="must be an absolute path"):
        normalize_db_path("state.db")


def test_normalize_db_path_normalizes_absolute_db(tmp_path: Path) -> None:
    db_path = normalize_db_path(str(tmp_path / "live" / "state_live.db"))

    assert db_path.is_absolute()
    assert db_path.suffix == ".db"
    assert db_path.parent.exists()


def test_enforce_role_db_convention_fails_for_live_when_missing_live(tmp_path: Path) -> None:
    db_path = tmp_path / "state_monitor.db"

    with pytest.raises(ValueError, match="LIVE"):
        enforce_role_db_convention("LIVE", False, db_path)


@pytest.mark.parametrize("process_role", ["MONITOR", "monitor"])
def test_enforce_role_db_convention_fails_for_monitor_when_missing_monitor(
    tmp_path: Path,
    process_role: str,
) -> None:
    db_path = tmp_path / "state_live.db"

    with pytest.raises(ValueError, match="MONITOR"):
        enforce_role_db_convention(process_role, False, db_path)


def test_enforce_role_db_convention_fails_when_live_trading_true_even_if_role_monitor(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state_monitor.db"

    with pytest.raises(ValueError, match="LIVE"):
        enforce_role_db_convention("MONITOR", True, db_path)
