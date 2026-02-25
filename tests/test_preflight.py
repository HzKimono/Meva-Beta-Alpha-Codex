from __future__ import annotations

from pathlib import Path

from btcbot.config import Settings
from btcbot.services.preflight import run_preflight_checks
from btcbot.services.state_store import StateStore


def _live_settings(db_path: Path) -> Settings:
    return Settings(
        DRY_RUN=False,
        LIVE_TRADING=True,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        SAFE_MODE=False,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(db_path),
        BTCTURK_API_KEY="x",
        BTCTURK_API_SECRET="y",
        MAX_NOTIONAL_PER_ORDER_TRY="100",
    )


def test_preflight_fails_for_live_when_kill_switch_enabled(tmp_path) -> None:
    settings = _live_settings(tmp_path / "state.db")
    settings.kill_switch = True

    report = run_preflight_checks(
        settings=settings,
        profile="live",
        auth_check=lambda _settings: (True, "ok"),
    )

    assert report["passed"] is False
    checks = {row["name"]: row for row in report["checks"]}
    assert checks["kill_switch_disabled_in_settings"]["ok"] is False


def test_preflight_fails_for_live_when_stage4_freeze_active(tmp_path) -> None:
    db_path = tmp_path / "freeze.db"
    settings = _live_settings(db_path)
    store = StateStore(str(db_path))
    store.stage4_set_freeze("LIVE", reason="unknown_open_orders", details={"count": 1})
    store.release_instance_lock()
    report = run_preflight_checks(
        settings=settings,
        profile="live",
        auth_check=lambda _settings: (True, "ok"),
    )

    assert report["passed"] is False
    checks = {row["name"]: row for row in report["checks"]}
    assert checks["stage4_freeze_inactive_live"]["ok"] is False


def test_preflight_fails_for_live_when_ack_missing(tmp_path) -> None:
    settings = Settings(
        DRY_RUN=True,
        STATE_DB_PATH=str(tmp_path / "state.db"),
        BTCTURK_API_KEY="x",
        BTCTURK_API_SECRET="y",
        MAX_NOTIONAL_PER_ORDER_TRY="100",
    )
    settings.live_trading = True
    settings.live_trading_ack = None
    settings.dry_run = False
    settings.safe_mode = False
    settings.kill_switch = False

    report = run_preflight_checks(
        settings=settings,
        profile="live",
        auth_check=lambda _settings: (True, "ok"),
    )

    assert report["passed"] is False
    checks = {row["name"]: row for row in report["checks"]}
    assert checks["live_env_armed"]["ok"] is False




def test_preflight_releases_strict_instance_lock_and_cleans_probe(tmp_path) -> None:
    db_path = tmp_path / "lock_release.db"
    settings = _live_settings(db_path)

    report = run_preflight_checks(
        settings=settings,
        profile="live",
        auth_check=lambda _settings: (True, "ok"),
    )

    assert report["passed"] is True

    second = StateStore(str(db_path), strict_instance_lock=True)
    with second._connect() as conn:
        probe_row = conn.execute(
            "SELECT key FROM op_state WHERE key = ?",
            ("preflight_probe",),
        ).fetchone()
    assert probe_row is None

def test_preflight_passes_for_dry_run_profile(tmp_path) -> None:
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=True,
        STATE_DB_PATH=str(tmp_path / "state.db"),
        MAX_NOTIONAL_PER_ORDER_TRY="100",
    )

    report = run_preflight_checks(
        settings=settings,
        profile="dry-run",
        auth_check=lambda _settings: (False, "should_be_skipped"),
    )

    assert report["profile"] == "dry-run"
    assert report["passed"] is True
