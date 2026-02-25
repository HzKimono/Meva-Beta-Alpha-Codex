from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.services.stage7_reporting import (
    CycleReportRow,
    build_cycle_rows,
    render_csv,
    rollup,
    validate_cycle_rows,
)
from btcbot.services.state_store import StateStore


def _run_metrics(ts: str, *, gross: str, net: str, fees: str, slippage: str, drawdown_pct: str) -> dict[str, object]:
    return {
        "ts": ts,
        "run_id": "r1",
        "mode_base": "NORMAL",
        "mode_final": "NORMAL",
        "universe_size": 2,
        "intents_planned_count": 2,
        "intents_skipped_count": 0,
        "oms_submitted_count": 2,
        "oms_filled_count": 1,
        "oms_rejected_count": 1,
        "oms_canceled_count": 0,
        "events_appended": 1,
        "events_ignored": 0,
        "equity_try": "1000",
        "gross_pnl_try": gross,
        "net_pnl_try": net,
        "fees_try": fees,
        "slippage_try": slippage,
        "max_drawdown_pct": drawdown_pct,
        "turnover_try": "500",
        "latency_ms_total": 1,
        "selection_ms": 1,
        "planning_ms": 1,
        "intents_ms": 1,
        "oms_ms": 1,
        "ledger_ms": 1,
        "persist_ms": 1,
        "quality_flags": {"throttled": False},
        "alert_flags": {"drawdown_breach": False},
    }


def _save_cycle(store: StateStore, cycle_id: str, ts: datetime, metrics: dict[str, object]) -> None:
    store.save_stage7_cycle(
        cycle_id=cycle_id,
        ts=ts,
        selected_universe=["BTCTRY", "ETHTRY"],
        universe_scores=[{"symbol": "BTCTRY", "score": "1.2"}],
        intents_summary={"planned": int(metrics["intents_planned_count"])},
        mode_payload={"base": metrics["mode_base"], "final": metrics["mode_final"]},
        order_decisions=[],
        portfolio_plan={},
        ledger_metrics={
            "gross_pnl_try": Decimal(str(metrics["gross_pnl_try"])),
            "realized_pnl_try": Decimal(str(metrics["gross_pnl_try"])),
            "unrealized_pnl_try": Decimal("0"),
            "net_pnl_try": Decimal(str(metrics["net_pnl_try"])),
            "fees_try": Decimal(str(metrics["fees_try"])),
            "slippage_try": Decimal(str(metrics["slippage_try"])),
            "turnover_try": Decimal(str(metrics["turnover_try"])),
            "equity_try": Decimal(str(metrics["equity_try"])),
            "max_drawdown": Decimal(str(metrics["max_drawdown_pct"])),
            "max_drawdown_ratio": Decimal(str(metrics["max_drawdown_pct"])),
        },
    )
    store.save_stage7_run_metrics(cycle_id, metrics)


def test_build_cycle_rows_roundtrip(tmp_path) -> None:
    store = StateStore(str(tmp_path / "stage7_reporting.db"))
    metrics = _run_metrics(
        "2024-01-01T00:00:00+00:00",
        gross="10.0",
        net="9.5",
        fees="0.3",
        slippage="0.2",
        drawdown_pct="10",
    )
    _save_cycle(store, "c1", datetime(2024, 1, 1, tzinfo=UTC), metrics)

    rows = build_cycle_rows(store, limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row.cycle_id == "c1"
    assert isinstance(row.gross_pnl_try, Decimal)
    assert row.gross_pnl_try == Decimal("10.0")
    assert row.max_drawdown_ratio == Decimal("0.1")
    assert row.max_drawdown_pct == Decimal("10")
    assert row.quality_flags == {"throttled": False}


def test_validation_net_pnl_identity() -> None:
    row = CycleReportRow(
        ts="2024-01-01T00:00:00+00:00",
        cycle_id="bad-1",
        run_id="r1",
        mode_base="NORMAL",
        mode_final="NORMAL",
        universe_size=1,
        gross_pnl_try=Decimal("10"),
        net_pnl_try=Decimal("20"),
        fees_try=Decimal("1"),
        slippage_try=Decimal("1"),
        turnover_try=Decimal("100"),
        equity_try=Decimal("1000"),
        max_drawdown_ratio=Decimal("0.1"),
        max_drawdown_pct=Decimal("10"),
        rejects=0,
        fill_rate=Decimal("1"),
        intents_planned_count=1,
        oms_submitted_count=1,
        oms_filled_count=1,
        quality_flags={},
        alert_flags={},
    )

    findings = validate_cycle_rows([row])

    assert any(item.code == "net_pnl_identity_mismatch" for item in findings)


def test_rollup_daily_weekly() -> None:
    rows = [
        CycleReportRow(
            ts="2024-01-01T00:00:00+00:00",
            cycle_id="c1",
            run_id="r1",
            mode_base="NORMAL",
            mode_final="NORMAL",
            universe_size=1,
            gross_pnl_try=Decimal("10"),
            net_pnl_try=Decimal("8"),
            fees_try=Decimal("1"),
            slippage_try=Decimal("1"),
            turnover_try=Decimal("100"),
            equity_try=Decimal("1000"),
            max_drawdown_ratio=Decimal("0.10"),
            max_drawdown_pct=Decimal("10"),
            rejects=1,
            fill_rate=Decimal("0.5"),
            intents_planned_count=1,
            oms_submitted_count=2,
            oms_filled_count=1,
            quality_flags={},
            alert_flags={},
        ),
        CycleReportRow(
            ts="2024-01-01T01:00:00+00:00",
            cycle_id="c2",
            run_id="r1",
            mode_base="NORMAL",
            mode_final="NORMAL",
            universe_size=1,
            gross_pnl_try=Decimal("20"),
            net_pnl_try=Decimal("19"),
            fees_try=Decimal("1"),
            slippage_try=Decimal("0"),
            turnover_try=Decimal("200"),
            equity_try=Decimal("1019"),
            max_drawdown_ratio=Decimal("0.20"),
            max_drawdown_pct=Decimal("20"),
            rejects=0,
            fill_rate=Decimal("1"),
            intents_planned_count=1,
            oms_submitted_count=1,
            oms_filled_count=1,
            quality_flags={},
            alert_flags={},
        ),
        CycleReportRow(
            ts="2024-01-03T00:00:00+00:00",
            cycle_id="c3",
            run_id="r1",
            mode_base="NORMAL",
            mode_final="NORMAL",
            universe_size=1,
            gross_pnl_try=Decimal("5"),
            net_pnl_try=Decimal("4.5"),
            fees_try=Decimal("0.5"),
            slippage_try=Decimal("0"),
            turnover_try=Decimal("50"),
            equity_try=Decimal("1023.5"),
            max_drawdown_ratio=Decimal("0.15"),
            max_drawdown_pct=Decimal("15"),
            rejects=2,
            fill_rate=Decimal("0.5"),
            intents_planned_count=1,
            oms_submitted_count=2,
            oms_filled_count=1,
            quality_flags={},
            alert_flags={},
        ),
    ]

    daily = rollup(rows, "daily")
    weekly = rollup(rows, "weekly")

    assert len(daily.buckets) == 2
    jan1 = next(bucket for bucket in daily.buckets if bucket.period_start.startswith("2024-01-01"))
    assert jan1.gross_pnl_try == Decimal("30")
    assert jan1.net_pnl_try == Decimal("27")
    assert jan1.rejects == 1
    assert len(weekly.buckets) == 1
    assert weekly.buckets[0].gross_pnl_try == Decimal("35")


def test_csv_schema_stable() -> None:
    rows = [
        CycleReportRow(
            ts="2024-01-01T00:00:00+00:00",
            cycle_id="c1",
            run_id="r1",
            mode_base="NORMAL",
            mode_final="NORMAL",
            universe_size=1,
            gross_pnl_try=Decimal("1"),
            net_pnl_try=Decimal("0.8"),
            fees_try=Decimal("0.1"),
            slippage_try=Decimal("0.1"),
            turnover_try=Decimal("10"),
            equity_try=Decimal("100"),
            max_drawdown_ratio=Decimal("0.01"),
            max_drawdown_pct=Decimal("1"),
            rejects=0,
            fill_rate=Decimal("1"),
            intents_planned_count=1,
            oms_submitted_count=1,
            oms_filled_count=1,
            quality_flags={"ok": True},
            alert_flags={"warn": False},
        )
    ]

    output = render_csv(rows)
    parsed = list(csv.reader(output.splitlines()))

    assert parsed[0] == [
        "ts",
        "cycle_id",
        "run_id",
        "mode_base",
        "mode_final",
        "universe_size",
        "gross_pnl_try",
        "net_pnl_try",
        "fees_try",
        "slippage_try",
        "turnover_try",
        "equity_try",
        "max_drawdown_ratio",
        "rejects",
        "fill_rate",
        "intents_planned_count",
        "oms_submitted_count",
        "oms_filled_count",
        "quality_flags_json",
        "alert_flags_json",
    ]
    assert len(parsed) == 2
