from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.domain.risk_budget import Mode, RiskDecision, RiskLimits, RiskSignals
from btcbot.persistence.uow import UnitOfWorkFactory


def _decision() -> RiskDecision:
    return RiskDecision(
        mode=Mode.NORMAL,
        reasons=["ok"],
        limits=RiskLimits(
            max_daily_drawdown_try=Decimal("10"),
            max_drawdown_try=Decimal("20"),
            max_gross_exposure_try=Decimal("100"),
            max_position_pct=Decimal("0.5"),
            max_order_notional_try=Decimal("5"),
            min_cash_try=Decimal("0"),
            max_fee_try_per_day=Decimal("5"),
        ),
        signals=RiskSignals(
            equity_try=Decimal("100"),
            peak_equity_try=Decimal("100"),
            drawdown_try=Decimal("0"),
            daily_pnl_try=Decimal("0"),
            gross_exposure_try=Decimal("0"),
            largest_position_pct=Decimal("0"),
            fees_try_today=Decimal("0"),
        ),
        decided_at=datetime.now(UTC),
    )


def test_uow_commit_and_rollback(tmp_path) -> None:
    db = tmp_path / "state.sqlite"
    factory = UnitOfWorkFactory(str(db))

    with factory() as uow:
        uow.trace.record_cycle_audit("c1", {"a": 1}, ["x"])

    with pytest.raises(RuntimeError):
        with factory() as uow:
            uow.trace.record_cycle_audit("c2", {"a": 2}, ["y"])
            raise RuntimeError("boom")

    with sqlite3.connect(db) as conn:
        c1 = conn.execute("SELECT COUNT(*) FROM cycle_audit WHERE cycle_id='c1'").fetchone()[0]
        c2 = conn.execute("SELECT COUNT(*) FROM cycle_audit WHERE cycle_id='c2'").fetchone()[0]
    assert c1 == 1
    assert c2 == 0


def test_risk_repo_isolation(tmp_path) -> None:
    db = tmp_path / "state.sqlite"
    factory = UnitOfWorkFactory(str(db))
    with factory() as uow:
        uow.risk.save_risk_decision(cycle_id="c1", decision=_decision(), prev_mode=None)

    with sqlite3.connect(db) as conn:
        risk_rows = conn.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
        metric_rows = conn.execute("SELECT COUNT(*) FROM cycle_metrics").fetchone()[0]
    assert risk_rows == 1
    assert metric_rows == 0


def test_read_only_guard_fails_closed(tmp_path) -> None:
    db = tmp_path / "state.sqlite"
    ro_factory = UnitOfWorkFactory(str(db), read_only=True)
    with pytest.raises(PermissionError):
        with ro_factory() as uow:
            uow.metrics.save_cycle_metrics(
                cycle_id="c1",
                ts_start="a",
                ts_end="b",
                mode="NORMAL",
                fills_count=0,
                orders_submitted=0,
                orders_canceled=0,
                rejects_count=0,
                fill_rate=0.0,
                avg_time_to_fill=None,
                slippage_bps_avg=None,
                fees_json="{}",
                pnl_json="{}",
                meta_json="{}",
            )
