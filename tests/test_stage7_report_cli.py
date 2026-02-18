import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from btcbot import cli
from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
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

    assert cli.run_stage7_report(settings, db_path=settings.state_db_path, last=5) == 0
    report = capsys.readouterr().out
    assert "cycle_id" in report
    assert "net_pnl_try" in report
    assert "c1" in report
    assert "1" in report

    assert cli.run_stage7_alerts(settings, db_path=settings.state_db_path, last=5) == 0
    alerts = capsys.readouterr().out
    assert "cycle_id ts alerts" in alerts


def test_stage7_export_jsonl(tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    out = tmp_path / "metrics.jsonl"
    assert (
        cli.run_stage7_export(
            settings,
            db_path=settings.state_db_path,
            last=5,
            export_format="jsonl",
            out_path=str(out),
        )
        == 0
    )
    assert out.exists()
    assert "cycle_id" in out.read_text(encoding="utf-8")


def test_stage7_alerts_all_false_only_header(capsys, tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    assert cli.run_stage7_alerts(settings, db_path=settings.state_db_path, last=5) == 0
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

    code = cli.run_stage7_db_count(settings=settings, db_path=settings.state_db_path)

    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "stage7_cycle_trace: 0" in lines
    assert "stage7_ledger_metrics: 0" in lines
    assert "stage7_run_metrics: 1" in lines
    assert "stage7_param_changes: 0" in lines
    assert "stage7_params_checkpoints: 0" in lines
    assert "stage7_params_active: 0" in lines


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
        settings=settings,
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


def test_stage7_doctor_reports_missing_dataset(capsys) -> None:
    settings = Settings()
    code = cli.run_doctor(settings, db_path=None, dataset_path="./does-not-exist")
    out = capsys.readouterr().out
    assert code == 2
    assert "dataset path does not exist" in out


def test_stage7_parity_missing_tables_does_not_crash(tmp_path: Path, capsys) -> None:
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    with sqlite3.connect(db_a) as conn:
        conn.execute("CREATE TABLE unrelated_a (id INTEGER PRIMARY KEY)")
    with sqlite3.connect(db_b) as conn:
        conn.execute("CREATE TABLE unrelated_b (id INTEGER PRIMARY KEY)")

    code = cli.run_stage7_parity(
        db_a=str(db_a),
        db_b=str(db_b),
        start="2024-01-01T00:00:00Z",
        end="2024-01-01T01:00:00Z",
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "missing Stage7 parity tables" in out


def test_stage7_report_prints_no_trade_reason_and_allocation_summary(
    capsys, tmp_path: Path
) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)
    store.save_stage7_run_metrics(
        "c2",
        {
            "ts": "2024-01-01T00:01:00+00:00",
            "run_id": "r2",
            "mode_base": "NORMAL",
            "mode_final": "NORMAL",
            "universe_size": 1,
            "intents_planned_count": 0,
            "intents_skipped_count": 1,
            "oms_submitted_count": 0,
            "oms_filled_count": 0,
            "oms_rejected_count": 0,
            "oms_canceled_count": 0,
            "events_appended": 0,
            "events_ignored": 0,
            "equity_try": "100",
            "gross_pnl_try": "0",
            "net_pnl_try": "0",
            "fees_try": "0",
            "slippage_try": "0",
            "max_drawdown_pct": "0",
            "turnover_try": "0",
            "latency_ms_total": 1,
            "selection_ms": 0,
            "planning_ms": 0,
            "intents_ms": 0,
            "oms_ms": 0,
            "ledger_ms": 0,
            "persist_ms": 0,
            "quality_flags": {},
            "alert_flags": {},
            "no_trades_reason": "NO_TRADE_PLANNING",
            "no_metrics_reason": "NO_TRADES",
        },
    )
    store.save_allocation_plan(
        cycle_id="c2",
        ts=__import__("datetime").datetime.now(__import__("datetime").UTC),
        cash_try=__import__("decimal").Decimal("500"),
        try_cash_target=__import__("decimal").Decimal("300"),
        investable_total_try=__import__("decimal").Decimal("200"),
        investable_this_cycle_try=__import__("decimal").Decimal("200"),
        deploy_budget_try=__import__("decimal").Decimal("200"),
        planned_total_try=__import__("decimal").Decimal("0"),
        unused_budget_try=__import__("decimal").Decimal("200"),
        usage_reason="use_all",
        plan=[{"symbol": "BTCTRY", "notional_try": "150", "side": "buy"}],
        deferred=[{"symbol": "ETHTRY", "reason": "max_orders_per_cycle"}],
        decisions=[],
    )

    assert cli.run_stage7_report(settings, db_path=settings.state_db_path, last=5) == 0
    out = capsys.readouterr().out
    assert "NO_TRADE_PLANNING" in out
    assert "stage4_plan_summary=planned_total_try=0" in out
    assert "selected_symbols=BTCTRY:150" in out
    assert "allocation_plan=source=cycle_id cycle_id=c2 investable_total_try=200" in out


class _Stage4ReportExchange:
    def get_orderbook(self, symbol: str) -> tuple[float, float]:
        del symbol
        return (100.0, 101.0)

    def get_balances(self):
        return [type("B", (), {"asset": "TRY", "free": Decimal("1000")})()]

    def list_open_orders(self, symbol: str):
        del symbol
        return []

    def get_recent_fills(self, symbol: str, since_ms: int | None = None):
        del symbol, since_ms
        return []

    def get_exchange_info(self):
        return [
            PairInfo(
                pairSymbol="BTCTRY",
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                tickSize=Decimal("0.1"),
                stepSize=Decimal("0.0001"),
            )
        ]

    def close(self) -> None:
        return None


def test_stage7_report_reads_stage4_plan_from_persisted_cycle(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    settings = Settings(
        STATE_DB_PATH=str(tmp_path / "s7-stage4.db"),
        DRY_RUN=True,
        KILL_SWITCH=True,
        SYMBOLS="BTC_TRY",
    )
    runner = Stage4CycleRunner()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: _Stage4ReportExchange(),
    )

    assert runner.run_one_cycle(settings) == 0

    store = StateStore(db_path=settings.state_db_path)
    with store._connect() as conn:
        plan_row = conn.execute(
            "SELECT cycle_id FROM allocation_plans ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    assert plan_row is not None
    cycle_id = str(plan_row["cycle_id"])

    store.save_stage7_run_metrics(
        cycle_id,
        {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": "r-stage4",
            "mode_base": "NORMAL",
            "mode_final": "NORMAL",
            "universe_size": 1,
            "intents_planned_count": 0,
            "intents_skipped_count": 0,
            "oms_submitted_count": 0,
            "oms_filled_count": 0,
            "oms_rejected_count": 0,
            "oms_canceled_count": 0,
            "events_appended": 0,
            "events_ignored": 0,
            "equity_try": "1000",
            "gross_pnl_try": "0",
            "net_pnl_try": "0",
            "fees_try": "0",
            "slippage_try": "0",
            "max_drawdown_pct": "0",
            "turnover_try": "0",
            "latency_ms_total": 1,
            "selection_ms": 0,
            "planning_ms": 0,
            "intents_ms": 0,
            "oms_ms": 0,
            "ledger_ms": 0,
            "persist_ms": 0,
            "quality_flags": {},
            "alert_flags": {},
            "no_trades_reason": "KILL_SWITCH",
            "no_metrics_reason": "NO_FILLS",
        },
    )

    assert cli.run_stage7_report(settings, db_path=settings.state_db_path, last=5) == 0
    out = capsys.readouterr().out
    assert "no_trades_reason" in out
    assert "KILL_SWITCH" in out
    assert "stage4_plan_summary=" in out
    assert "allocation_plan=source=cycle_id" in out


def test_report_includes_slo_status(capsys, tmp_path: Path) -> None:
    settings = Settings(STATE_DB_PATH=str(tmp_path / "s7.db"))
    store = StateStore(db_path=settings.state_db_path)
    _seed_metrics(store)

    assert cli.run_stage7_report(settings, db_path=settings.state_db_path, last=5) == 0
    report = capsys.readouterr().out
    assert "slo_status" in report
    assert "window_status=PASS" in report

    assert (
        cli.run_stage7_report(settings, db_path=settings.state_db_path, last=5, json_output=True)
        == 0
    )
    payload = capsys.readouterr().out
    assert '"summary"' in payload
    assert '"slo_status"' in payload
