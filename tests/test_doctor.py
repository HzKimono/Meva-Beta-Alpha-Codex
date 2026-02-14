from __future__ import annotations

import json
from pathlib import Path

import pytest

from btcbot import cli
from btcbot.config import Settings
from btcbot.services.doctor import DoctorCheck, DoctorReport, run_health_checks


def test_doctor_report_ok_true_when_all_checks_pass() -> None:
    report = DoctorReport(
        checks=[DoctorCheck("gates", "coherence", "pass", "ok")],
        errors=["legacy error list should not affect ok"],
        warnings=[],
        actions=[],
    )

    assert report.ok


def test_doctor_report_ok_true_when_warn_present() -> None:
    report = DoctorReport(
        checks=[
            DoctorCheck("gates", "coherence", "pass", "ok"),
            DoctorCheck("paths", "db", "warn", "warning only"),
        ],
        errors=[],
        warnings=["warning only"],
        actions=[],
    )

    assert report.ok


def test_doctor_report_ok_false_when_any_check_fails() -> None:
    report = DoctorReport(
        checks=[
            DoctorCheck("gates", "coherence", "pass", "ok"),
            DoctorCheck("exchange_rules", "symbols", "fail", "broken"),
        ],
        errors=[],
        warnings=[],
        actions=[],
    )

    assert not report.ok


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


class _DoctorExchange:
    def __init__(self, rows):
        self._rows = rows

    def get_exchange_info(self):
        return self._rows

    def close(self) -> None:
        return None


@pytest.fixture
def patch_doctor_exchange(monkeypatch):
    def _apply(rows):
        monkeypatch.setattr(
            "btcbot.services.doctor.build_exchange_stage4",
            lambda settings, dry_run: _DoctorExchange(rows),
        )

    return _apply


def test_doctor_exchange_rules_check_passes_with_usable_rules(patch_doctor_exchange) -> None:
    patch_doctor_exchange(
        [
            {
                "name": "BTCTRY",
                "nameNormalized": "BTC_TRY",
                "numeratorScale": 8,
                "denominatorScale": 2,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": "1",
                        "minExchangeValue": "99.91",
                    }
                ],
            }
        ]
    )
    report = run_health_checks(Settings(SYMBOLS=["BTC_TRY"]), db_path=None, dataset_path=None)

    assert any(
        check.category == "exchange_rules" and check.status == "pass" for check in report.checks
    )


def test_doctor_exchange_rules_check_fails_for_invalid_rules(patch_doctor_exchange) -> None:
    patch_doctor_exchange(
        [
            {
                "name": "BTCTRY",
                "nameNormalized": "BTC_TRY",
                "numeratorScale": 8,
                "denominatorScale": 2,
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0"}],
            }
        ]
    )
    report = run_health_checks(Settings(SYMBOLS=["BTC_TRY"]), db_path=None, dataset_path=None)

    assert not report.ok
    assert any("exchange rules unusable" in error for error in report.errors)
    assert any("exchangeinfo schema" in action.lower() for action in report.actions)
