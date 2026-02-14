import sys
from pathlib import Path

from btcbot import cli
from btcbot.config import Settings
from btcbot.services.state_store import StateStore


def _seed_metrics(store: StateStore) -> None:
    store.save_stage7_run_metrics(
        "c1",
        {
            "ts": "2024-01-01T00:00:00+00:00",
            "run_id": "r1",
            "mode_base": "NORMAL",
            "mode_final": "NORMAL",
            "universe_size": 1,
            "intents_planned_count": 1,
            "intents_skipped_count": 0,
            "oms_submitted_count": 1,
            "oms_filled_count": 1,
            "oms_rejected_count": 0,
            "oms_canceled_count": 0,
            "events_appended": 1,
            "events_ignored": 0,
            "oms_throttled_count": 0,
            "equity_try": "100",
            "gross_pnl_try": "2",
            "net_pnl_try": "1",
            "fees_try": "0.2",
            "slippage_try": "0.1",
            "max_drawdown_pct": "0.1",
            "turnover_try": "50",
            "latency_ms_total": 1,
            "selection_ms": 0,
            "planning_ms": 0,
            "intents_ms": 0,
            "oms_ms": 0,
            "ledger_ms": 0,
            "persist_ms": 0,
            "quality_flags": {"throttled": False},
            "alert_flags": {"drawdown_breach": False},
        },
    )


def test_stage7_report_and_alerts(capsys, tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    assert cli.run_stage7_report(settings, last=5) == 0
    report = capsys.readouterr().out
    assert "cycle_id" in report
    assert "net_pnl_try" in report
    assert "c1" in report
    assert "1" in report

    assert cli.run_stage7_alerts(settings, last=5) == 0
    alerts = capsys.readouterr().out
    assert "cycle_id ts alerts" in alerts


def test_stage7_export_jsonl(tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    out = tmp_path / "metrics.jsonl"
    assert cli.run_stage7_export(settings, last=5, export_format="jsonl", out_path=str(out)) == 0
    assert out.exists()
    assert "cycle_id" in out.read_text(encoding="utf-8")


def test_stage7_alerts_all_false_only_header(capsys, tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    assert cli.run_stage7_alerts(settings, last=5) == 0
    lines = capsys.readouterr().out.strip().splitlines()

    assert lines == ["cycle_id ts alerts"]


def test_stage7_parity_dataset_message(capsys) -> None:
    code = cli.run_stage7_parity(
        db_a="a.db",
        db_b="b.db",
        start="2024-01-01T00:00:00Z",
        end="2024-01-01T01:00:00Z",
        dataset="./data",
    )

    assert code == 2
    out = capsys.readouterr().out.strip()
    assert "stage7-parity compares two DBs" in out
    assert "stage7-backtest" in out


def test_stage7_db_count_reports_existing_and_missing_tables(capsys, tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    code = cli.run_stage7_db_count(db_path=settings.state_db_path)

    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "stage7_cycle_trace: 0" in lines
    assert "stage7_ledger_metrics: 0" in lines
    assert "stage7_run_metrics: 1" in lines
    assert "stage7_param_changes: 0" in lines


def test_main_stage7_backtest_report_alias(monkeypatch, tmp_path: Path) -> None:
    class FakeSettings:
        log_level = "INFO"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)

    def _fake_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_stage7_backtest_export", _fake_export)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "btcbot",
            "stage7-backtest-report",
            "--db",
            str(tmp_path / "in.db"),
            "--out",
            str(tmp_path / "out.jsonl"),
        ],
    )

    assert cli.main() == 0
    assert captured["db_path"].endswith("in.db")


def test_stage7_backtest_export_warns_when_last_is_implicit(capsys, tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    out = tmp_path / "metrics.jsonl"
    code = cli.run_stage7_backtest_export(
        db_path=settings.state_db_path,
        last=50,
        export_format="jsonl",
        out_path=str(out),
        explicit_last=False,
    )

    assert code == 0
    stderr = capsys.readouterr().err
    assert "exporting last 50 rows" in stderr


def test_stage7_parity_invalid_quantize_returns_code_2(capsys) -> None:
    code = cli.run_stage7_parity(
        db_a="a.db",
        db_b="b.db",
        start="2024-01-01T00:00:00Z",
        end="2024-01-01T01:00:00Z",
        quantize_try="not-a-decimal",
    )

    assert code == 2
    assert "--quantize-try must be a decimal" in capsys.readouterr().out
