from __future__ import annotations

import json
from pathlib import Path

from btcbot import cli
from btcbot.config import Settings
from btcbot.services.doctor import run_health_checks


def test_doctor_accepts_creatable_db_path(tmp_path: Path) -> None:
    report = run_health_checks(
        Settings(),
        db_path=str(tmp_path / "nested" / "new.db"),
        dataset_path=None,
    )

    assert report.ok
    assert any("schema_version table missing" in warning for warning in report.warnings)


def test_doctor_fails_when_dataset_is_file(tmp_path: Path) -> None:
    dataset_file = tmp_path / "dataset.txt"
    dataset_file.write_text("x", encoding="utf-8")

    report = run_health_checks(Settings(), db_path=None, dataset_path=str(dataset_file))

    assert not report.ok
    assert any("dataset path is not a directory" in error for error in report.errors)


def test_doctor_dataset_optional_ok(capsys) -> None:
    code = cli.run_doctor(
        Settings(), db_path="btcbot_state.db", dataset_path=None, json_output=False
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "dataset is optional; required only for replay/backtest" in out


def test_doctor_missing_dataset_fail_actions(capsys) -> None:
    code = cli.run_doctor(
        Settings(),
        db_path="btcbot_state.db",
        dataset_path="./does-not-exist",
        json_output=False,
    )

    out = capsys.readouterr().out
    assert code == 1
    assert r"Create folder: .\data\replay" in out
    assert "Or omit --dataset if you only run stage7-run." in out

    json_code = cli.run_doctor(
        Settings(),
        db_path="btcbot_state.db",
        dataset_path="./does-not-exist",
        json_output=True,
    )
    json_out = capsys.readouterr().out.strip()
    payload = json.loads(json_out)
    assert json_code == 1
    assert payload["status"] == "fail"
    assert any("replay-init" in action for action in payload["actions"])


def test_doctor_detects_stage7_gate_conflicts() -> None:
    settings = Settings(STAGE7_ENABLED=True, DRY_RUN=True, LIVE_TRADING=False)
    report = run_health_checks(settings, db_path=None, dataset_path=None)
    assert report.ok

    unsafe = Settings(STAGE7_ENABLED=False, DRY_RUN=True, LIVE_TRADING=True)
    unsafe_report = run_health_checks(unsafe, db_path=None, dataset_path=None)
    assert not unsafe_report.ok
    assert any("LIVE_TRADING=true" in error for error in unsafe_report.errors)
