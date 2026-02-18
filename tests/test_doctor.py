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


def test_doctor_dataset_optional_ok(
    capsys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "btcbot.services.doctor.resolve_effective_universe",
        lambda settings: type(
            "EffectiveUniverse",
            (),
            {
                "symbols": settings.symbols,
                "source": "settings",
                "metadata_available": True,
                "rejected_symbols": [],
                "suggestions": [],
                "auto_corrected_symbols": [],
            },
        )(),
    )
    monkeypatch.setattr(
        "btcbot.services.doctor._check_exchange_rules",
        lambda settings, symbols, checks, errors, warnings, actions: checks.append(
            DoctorCheck("exchange_rules", "compatibility", "pass", "ok")
        ),
    )

    db_path = tmp_path / "doctor_state.db"
    code = cli.run_doctor(
        Settings(DOCTOR_SLO_ENABLED=False),
        db_path=str(db_path),
        dataset_path=None,
        json_output=False,
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
    assert code == 2
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
    assert json_code == 2
    assert payload["status"] == "FAIL"
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


def _seed_stage7_row(
    store,
    *,
    cycle_id: str,
    ts: str,
    submitted: int,
    filled: int,
    rejected: int,
    latency_ms_total: int,
    drawdown_pct: str = "0.01",
    drawdown_ratio: str | None = "0.01",
) -> None:
    metrics = {
        "ts": ts,
        "run_id": "r1",
        "mode_base": "NORMAL",
        "mode_final": "NORMAL",
        "universe_size": 1,
        "intents_planned_count": 1,
        "intents_skipped_count": 0,
        "oms_submitted_count": submitted,
        "oms_filled_count": filled,
        "oms_rejected_count": rejected,
        "oms_canceled_count": 0,
        "events_appended": 1,
        "events_ignored": 0,
        "equity_try": "100",
        "gross_pnl_try": "2",
        "net_pnl_try": "1",
        "fees_try": "0.2",
        "slippage_try": "0.1",
        "max_drawdown_pct": drawdown_pct,
        "turnover_try": "50",
        "latency_ms_total": latency_ms_total,
        "selection_ms": 0,
        "planning_ms": 0,
        "intents_ms": 0,
        "oms_ms": 0,
        "ledger_ms": 0,
        "persist_ms": 0,
        "quality_flags": {"throttled": False},
        "alert_flags": {"drawdown_breach": False},
    }
    if drawdown_ratio is not None:
        metrics["max_drawdown_ratio"] = drawdown_ratio
    store.save_stage7_run_metrics(cycle_id, metrics)


def test_doctor_exit_codes(capsys, tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "doctor.db")
    from btcbot.services.effective_universe import EffectiveUniverse
    from btcbot.services.state_store import StateStore

    monkeypatch.setattr(
        "btcbot.services.doctor.resolve_effective_universe",
        lambda settings: EffectiveUniverse(
            symbols=["BTCTRY"],
            rejected_symbols=[],
            metadata_available=True,
            source="test",
            suggestions={},
            auto_corrected_symbols={},
        ),
    )
    monkeypatch.setattr(
        "btcbot.services.doctor.build_exchange_stage4",
        lambda settings, dry_run: _DoctorExchange(
            [
                {
                    "name": "BTCTRY",
                    "nameNormalized": "BTC_TRY",
                    "numeratorScale": 8,
                    "denominatorScale": 2,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "1", "minExchangeValue": "100"}
                    ],
                }
            ]
        ),
    )

    store = StateStore(db_path=db)
    _seed_stage7_row(
        store,
        cycle_id="pass-1",
        ts="2024-01-01T00:00:00+00:00",
        submitted=10,
        filled=10,
        rejected=0,
        latency_ms_total=10,
    )
    assert cli.run_doctor(Settings(STATE_DB_PATH=db), db_path=db, dataset_path=None) == 0

    _seed_stage7_row(
        store,
        cycle_id="warn-1",
        ts="2024-01-01T00:01:00+00:00",
        submitted=10,
        filled=8,
        rejected=2,
        latency_ms_total=10,
    )
    assert cli.run_doctor(Settings(STATE_DB_PATH=db), db_path=db, dataset_path=None) == 1

    _seed_stage7_row(
        store,
        cycle_id="fail-1",
        ts="2024-01-01T00:02:00+00:00",
        submitted=10,
        filled=6,
        rejected=4,
        latency_ms_total=5000,
    )
    assert cli.run_doctor(Settings(STATE_DB_PATH=db), db_path=db, dataset_path=None) == 2
    out = capsys.readouterr().out
    assert "doctor_status=FAIL" in out


def test_doctor_slo_no_db_path_warn() -> None:
    report = run_health_checks(Settings(), db_path=None, dataset_path=None)
    checks = [c for c in report.checks if c.category == "slo" and c.name == "coverage"]
    assert checks
    assert checks[-1].status == "warn"


def test_doctor_slo_no_metrics_warn(tmp_path: Path) -> None:
    db = str(tmp_path / "empty.db")
    from btcbot.services.state_store import StateStore

    StateStore(db_path=db)
    report = run_health_checks(Settings(STATE_DB_PATH=db), db_path=db, dataset_path=None)
    slo_coverage = [c for c in report.checks if c.category == "slo" and c.name == "coverage"]
    assert slo_coverage
    assert slo_coverage[-1].status == "warn"
    assert "no stage7 metrics found" in slo_coverage[-1].message


def test_doctor_slo_fail_on_reject_rate(tmp_path: Path) -> None:
    db = str(tmp_path / "reject.db")
    from btcbot.services.state_store import StateStore

    store = StateStore(db_path=db)
    _seed_stage7_row(
        store,
        cycle_id="r-1",
        ts="2024-01-01T00:00:00+00:00",
        submitted=10,
        filled=7,
        rejected=3,
        latency_ms_total=50,
    )
    report = run_health_checks(Settings(STATE_DB_PATH=db), db_path=db, dataset_path=None)
    reject_checks = [c for c in report.checks if c.category == "slo" and c.name == "reject_rate"]
    assert reject_checks
    assert reject_checks[-1].status == "fail"


def test_doctor_drawdown_pct_normalizes_percent_value() -> None:
    row = {
        "oms_submitted_count": 10,
        "oms_filled_count": 10,
        "oms_rejected_count": 0,
        "latency_ms_total": 10,
        "max_drawdown_pct": "12.0",
    }
    from btcbot.services.doctor import evaluate_slo_status_for_rows

    _status, metrics, _violations = evaluate_slo_status_for_rows(
        Settings(),
        [row],
        drawdown_ratio=None,
    )
    assert metrics["max_drawdown_ratio"] == pytest.approx(0.12)


def test_doctor_drawdown_pct_normalizes_ratio_style_value() -> None:
    row = {
        "oms_submitted_count": 10,
        "oms_filled_count": 10,
        "oms_rejected_count": 0,
        "latency_ms_total": 10,
        "max_drawdown_pct": "0.12",
    }
    from btcbot.services.doctor import evaluate_slo_status_for_rows

    _status, metrics, _violations = evaluate_slo_status_for_rows(
        Settings(),
        [row],
        drawdown_ratio=None,
    )
    assert metrics["max_drawdown_ratio"] == pytest.approx(0.12)


def test_main_doctor_without_stage7_db_is_warn(monkeypatch, capsys) -> None:
    from btcbot.services.effective_universe import EffectiveUniverse

    monkeypatch.setattr(cli, "_resolve_stage7_db_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "btcbot.services.doctor.resolve_effective_universe",
        lambda settings: EffectiveUniverse(
            symbols=["BTCTRY"],
            rejected_symbols=[],
            metadata_available=True,
            source="test",
            suggestions={},
            auto_corrected_symbols={},
        ),
    )
    monkeypatch.setattr(
        "btcbot.services.doctor.build_exchange_stage4",
        lambda settings, dry_run: _DoctorExchange(
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
                            "minExchangeValue": "100",
                        }
                    ],
                }
            ]
        ),
    )
    monkeypatch.setattr(sys, "argv", ["btcbot", "doctor", "--json"])

    code = cli.main()

    assert code == 1
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "WARN"
    assert any(
        check["category"] == "slo" and check["name"] == "coverage" and check["status"] == "warn"
        for check in payload["checks"]
    )
