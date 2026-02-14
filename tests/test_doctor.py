from __future__ import annotations

from pathlib import Path

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


def test_doctor_detects_stage7_gate_conflicts() -> None:
    settings = Settings(STAGE7_ENABLED=True, DRY_RUN=True, LIVE_TRADING=False)
    report = run_health_checks(settings, db_path=None, dataset_path=None)
    assert report.ok

    unsafe = Settings(STAGE7_ENABLED=False, DRY_RUN=True, LIVE_TRADING=True)
    # LIVE_TRADING requires explicit arming and credentials.
    unsafe_report = run_health_checks(unsafe, db_path=None, dataset_path=None)
    assert not unsafe_report.ok
    assert any("LIVE_TRADING=true" in error for error in unsafe_report.errors)
