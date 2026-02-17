from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from btcbot import cli
from btcbot.config import Settings


class _DoctorReport:
    def __init__(self, status: str) -> None:
        self._status = status


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        STATE_DB_PATH=str(tmp_path / "state.db"),
        UNIVERSE_SYMBOLS='["BTCTRY"]',
        LIVE_TRADING=True,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        BTCTURK_API_KEY="test-key",
        BTCTURK_API_SECRET="test-secret",
        DRY_RUN=False,
        KILL_SWITCH=False,
        SAFE_MODE=False,
    )


def test_canary_aborts_on_doctor_fail(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    called = {"run_cycle": 0}

    monkeypatch.setattr(cli, "run_health_checks", lambda *args, **kwargs: _DoctorReport("fail"))
    monkeypatch.setattr(cli, "doctor_status", lambda report: report._status)
    monkeypatch.setattr(cli, "run_cycle", lambda *args, **kwargs: called.__setitem__("run_cycle", 1) or 0)
    monkeypatch.setattr(cli, "_check_canary_min_notional", lambda *args, **kwargs: (True, ""))

    rc = cli.run_canary(
        settings,
        mode="once",
        symbol="BTCTRY",
        notional_try=Decimal("150"),
        cycle_seconds=0,
        max_cycles=None,
        ttl_seconds=30,
        db_path=str(tmp_path / "state.db"),
        market_data_mode=None,
        allow_warn=False,
        export_out=None,
    )

    assert rc == 2
    assert called["run_cycle"] == 0


def test_canary_aborts_on_doctor_warn_without_allow_warn(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    called = {"run_cycle": 0}

    monkeypatch.setattr(cli, "run_health_checks", lambda *args, **kwargs: _DoctorReport("warn"))
    monkeypatch.setattr(cli, "doctor_status", lambda report: report._status)
    monkeypatch.setattr(cli, "run_cycle", lambda *args, **kwargs: called.__setitem__("run_cycle", 1) or 0)
    monkeypatch.setattr(cli, "_check_canary_min_notional", lambda *args, **kwargs: (True, ""))

    rc = cli.run_canary(
        settings,
        mode="once",
        symbol="BTCTRY",
        notional_try=Decimal("150"),
        cycle_seconds=0,
        max_cycles=None,
        ttl_seconds=30,
        db_path=str(tmp_path / "state.db"),
        market_data_mode=None,
        allow_warn=False,
        export_out=None,
    )

    assert rc == 1
    assert called["run_cycle"] == 0


def test_canary_proceeds_on_pass_and_forces_caps(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "run_health_checks", lambda *args, **kwargs: _DoctorReport("pass"))
    monkeypatch.setattr(cli, "doctor_status", lambda report: report._status)
    monkeypatch.setattr(cli, "_check_canary_min_notional", lambda *args, **kwargs: (True, ""))

    def _fake_run_cycle(effective_settings: Settings, force_dry_run: bool = False) -> int:
        captured["settings"] = effective_settings
        captured["force_dry_run"] = force_dry_run
        return 0

    monkeypatch.setattr(cli, "run_cycle", _fake_run_cycle)

    rc = cli.run_canary(
        settings,
        mode="once",
        symbol="BTCTRY",
        notional_try=Decimal("175"),
        cycle_seconds=0,
        max_cycles=None,
        ttl_seconds=25,
        db_path=str(tmp_path / "state.db"),
        market_data_mode="ws",
        allow_warn=False,
        export_out=None,
    )

    assert rc == 0
    effective = captured["settings"]
    assert isinstance(effective, Settings)
    assert effective.max_orders_per_cycle == 1
    assert effective.max_open_orders_per_symbol == 1
    assert effective.symbols == ["BTCTRY"]
    assert effective.ttl_seconds == 25
    assert effective.notional_cap_try_per_cycle == Decimal("175")
    assert effective.max_notional_per_order_try == Decimal("175")
    assert effective.market_data_mode == "ws"


def test_canary_loop_hard_stops_on_doctor_fail_midway(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    doctor_states = iter(["pass", "fail"])
    calls = {"run_cycle": 0}

    monkeypatch.setattr(
        cli,
        "run_health_checks",
        lambda *args, **kwargs: _DoctorReport(next(doctor_states)),
    )
    monkeypatch.setattr(cli, "doctor_status", lambda report: report._status)
    monkeypatch.setattr(cli, "_check_canary_min_notional", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    def _fake_run_cycle(*args, **kwargs) -> int:
        calls["run_cycle"] += 1
        return 0

    monkeypatch.setattr(cli, "run_cycle", _fake_run_cycle)

    rc = cli.run_canary(
        settings,
        mode="loop",
        symbol="BTCTRY",
        notional_try=Decimal("150"),
        cycle_seconds=0,
        max_cycles=10,
        ttl_seconds=30,
        db_path=str(tmp_path / "state.db"),
        market_data_mode=None,
        allow_warn=False,
        export_out=None,
    )

    assert rc == 2
    assert calls["run_cycle"] == 5


def test_canary_requires_db_path(monkeypatch, capsys, tmp_path: Path) -> None:
    settings = Settings(
        STATE_DB_PATH="",
        UNIVERSE_SYMBOLS='["BTCTRY"]',
        LIVE_TRADING=True,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        BTCTURK_API_KEY="test-key",
        BTCTURK_API_SECRET="test-secret",
        DRY_RUN=False,
        KILL_SWITCH=False,
        SAFE_MODE=False,
    )
    called = {"run_cycle": 0}

    monkeypatch.setattr(cli, "_load_settings", lambda _env_file: settings)
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(cli, "configure_instrumentation", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_apply_effective_universe", lambda s: s)
    monkeypatch.setattr(cli, "run_cycle", lambda *args, **kwargs: called.__setitem__("run_cycle", 1) or 0)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "btcbot",
            "canary",
            "once",
            "--symbol",
            "BTCTRY",
            "--notional-try",
            "150",
            "--ttl-seconds",
            "30",
        ],
    )

    rc = cli.main()

    assert rc == 2
    assert "missing DB path" in capsys.readouterr().out
    assert called["run_cycle"] == 0


def test_canary_acquires_single_instance_lock(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    lock_calls: list[tuple[str, str]] = []

    @contextmanager
    def _fake_lock(*, db_path: str, account_key: str):
        lock_calls.append((db_path, account_key))
        yield

    monkeypatch.setattr(cli, "single_instance_lock", _fake_lock)
    monkeypatch.setattr(cli, "run_health_checks", lambda *args, **kwargs: _DoctorReport("pass"))
    monkeypatch.setattr(cli, "doctor_status", lambda report: report._status)
    monkeypatch.setattr(cli, "_check_canary_min_notional", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(cli, "run_cycle", lambda *args, **kwargs: 0)

    resolved_db = str(tmp_path / "state.db")
    rc = cli.run_canary(
        settings,
        mode="once",
        symbol="BTCTRY",
        notional_try=Decimal("150"),
        cycle_seconds=0,
        max_cycles=None,
        ttl_seconds=30,
        db_path=resolved_db,
        market_data_mode=None,
        allow_warn=False,
        export_out=None,
    )

    assert rc == 0
    assert lock_calls == [(resolved_db, "canary")]
