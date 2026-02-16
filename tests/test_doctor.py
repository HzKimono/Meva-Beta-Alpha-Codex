from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from btcbot import cli
from btcbot.config import Settings
from btcbot.services.doctor import DoctorCheck, DoctorReport, run_health_checks


def _fixture_symbols(name: str) -> list[dict[str, object]]:
    payload = json.loads((Path("tests/fixtures") / name).read_text(encoding="utf-8"))
    return list(payload["data"]["symbols"])


def _debug_checks(report: DoctorReport) -> None:
    rendered = [f"{check.category}/{check.name}:{check.status}" for check in report.checks]
    print(f"doctor checks => {rendered}", file=sys.stderr)


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

    _debug_checks(report)
    assert report.ok
    assert any("schema_version table missing" in warning for warning in report.warnings)


def test_doctor_fails_when_dataset_is_file(tmp_path: Path) -> None:
    dataset_file = tmp_path / "dataset.txt"
    dataset_file.write_text("x", encoding="utf-8")

    report = run_health_checks(Settings(), db_path=None, dataset_path=str(dataset_file))

    _debug_checks(report)
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
    _debug_checks(report)
    assert report.ok

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(STAGE7_ENABLED=False, DRY_RUN=True, LIVE_TRADING=True)


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

    _debug_checks(report)
    assert any(
        check.category == "exchange_rules" and check.status == "pass" for check in report.checks
    )


def test_doctor_exchange_rules_check_warns_for_invalid_rules_in_non_blocking_mode(
    patch_doctor_exchange,
) -> None:
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

    _debug_checks(report)
    assert report.ok
    assert any("exchange rules unusable" in warning for warning in report.warnings)
    assert any("exchangeinfo schema" in action.lower() for action in report.actions)


def test_doctor_exchange_rules_check_fails_for_invalid_rules_in_blocking_mode(
    patch_doctor_exchange,
) -> None:
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
    settings = Settings(SYMBOLS=["BTC_TRY"])
    settings.stage7_enabled = True
    settings.live_trading = True

    report = run_health_checks(
        settings,
        db_path=None,
        dataset_path=None,
    )

    _debug_checks(report)
    assert not report.ok
    assert any("exchange rules unusable" in error for error in report.errors)
    assert any("exchangeinfo schema" in action.lower() for action in report.actions)


def test_doctor_exchange_rules_reports_actionable_reason(patch_doctor_exchange) -> None:
    patch_doctor_exchange(
        [
            {
                "name": "BTCTRY",
                "nameNormalized": "BTC_TRY",
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0"}],
            }
        ]
    )
    report = run_health_checks(Settings(SYMBOLS=["BTC_TRY"]), db_path=None, dataset_path=None)

    messages = [c.message for c in report.checks if c.category == "exchange_rules"]
    assert any("status=invalid_metadata:" in msg for msg in messages)
    assert any("invalid=" in msg and "tick_size" in msg for msg in messages)


def test_doctor_exchange_rules_passes_with_fixture_min_notional_variant(
    patch_doctor_exchange,
) -> None:
    patch_doctor_exchange(_fixture_symbols("btcturk_exchangeinfo_min_notional_present.json"))

    report = run_health_checks(Settings(SYMBOLS=["BTC_TRY"]), db_path=None, dataset_path=None)

    exchange_checks = [c for c in report.checks if c.category == "exchange_rules"]
    assert any(c.status == "pass" for c in exchange_checks)


def test_doctor_exchange_rules_warns_explicitly_for_absent_non_try_min_notional(
    patch_doctor_exchange,
) -> None:
    patch_doctor_exchange(_fixture_symbols("btcturk_exchangeinfo_min_notional_absent.json"))

    report = run_health_checks(Settings(SYMBOLS=["BTC_USDT"]), db_path=None, dataset_path=None)

    messages = [c.message for c in report.checks if c.category == "exchange_rules"]
    assert any("status=invalid_metadata:" in msg for msg in messages)
    assert any("safe_behavior=reject_and_continue" in msg for msg in messages)


def test_doctor_reports_effective_universe_and_source(monkeypatch) -> None:
    from btcbot.services.effective_universe import EffectiveUniverse

    monkeypatch.setattr(
        "btcbot.services.doctor.resolve_effective_universe",
        lambda settings: EffectiveUniverse(
            symbols=["BTCTRY"],
            rejected_symbols=["INVALIDTRY"],
            metadata_available=True,
            source="env:UNIVERSE_SYMBOLS",
            suggestions={"INVALIDTRY": ["XRPTRY"]},
            auto_corrected_symbols={},
        ),
    )

    report = run_health_checks(
        Settings(UNIVERSE_SYMBOLS="BTCTRY,INVALIDTRY"), db_path=None, dataset_path=None
    )

    messages = [c.message for c in report.checks if c.category == "universe"]
    assert any("source=env:UNIVERSE_SYMBOLS" in m for m in messages)
    assert any("size=1" in m for m in messages)
    assert any("suggested={'INVALIDTRY': ['XRPTRY']}" in m for m in messages)
    assert any("metadata validation performed" in m for m in messages)
    assert any("INVALIDTRY" in w for w in report.warnings)
