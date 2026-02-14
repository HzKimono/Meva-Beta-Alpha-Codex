from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from btcbot.config import Settings
from btcbot.services.adaptation_service import AdaptationService
from btcbot.services.state_store import StateStore


def _metric(
    ts: str, *, mode: str = "NORMAL", drawdown=False, reject=False, throttled=False, pnl="10"
):
    return {
        "ts": ts,
        "mode_base": mode,
        "mode_final": mode,
        "universe_size": 20,
        "intents_planned_count": 1,
        "intents_skipped_count": 0,
        "oms_submitted_count": 1,
        "oms_filled_count": 1,
        "oms_rejected_count": 4 if reject else 0,
        "oms_canceled_count": 0,
        "events_appended": 0,
        "events_ignored": 0,
        "equity_try": "1000",
        "gross_pnl_try": pnl,
        "net_pnl_try": pnl,
        "fees_try": "0",
        "slippage_try": "0",
        "max_drawdown_pct": "0.9" if drawdown else "0",
        "turnover_try": "100",
        "latency_ms_total": 1,
        "selection_ms": 1,
        "planning_ms": 1,
        "intents_ms": 1,
        "oms_ms": 1,
        "ledger_ms": 1,
        "persist_ms": 1,
        "quality_flags": {"throttled": throttled},
        "alert_flags": {
            "drawdown_breach": drawdown,
            "reject_spike": reject,
            "throttled": throttled,
        },
        "run_id": "r",
    }


def test_apply_reject_and_rollback_paths(tmp_path) -> None:
    db = tmp_path / "adapt.db"
    store = StateStore(str(db))
    settings = Settings(STATE_DB_PATH=str(db), STAGE7_ENABLED=True, DRY_RUN=True)
    svc = AdaptationService()
    now = datetime.now(UTC)
    active = store.get_active_stage7_params(settings=settings, now_utc=now)
    assert active.version == 1

    # healthy apply
    for i in range(3):
        store.save_stage7_run_metrics(f"c{i}", _metric(f"2024-01-01T00:00:0{i}+00:00"))
    applied = svc.evaluate_and_apply(state_store=store, settings=settings, now_utc=now)
    assert applied is not None and applied.outcome == "APPLIED"
    assert store.get_active_stage7_params(settings=settings, now_utc=now).version == 2

    # reject due to non-normal mode
    store.save_stage7_run_metrics("cx", _metric("2024-01-01T00:00:10+00:00", mode="OBSERVE_ONLY"))
    rejected = svc.evaluate_and_apply(state_store=store, settings=settings, now_utc=now)
    assert rejected is not None and rejected.outcome == "REJECTED"

    # rollback on drawdown breach
    store.save_stage7_run_metrics("cy", _metric("2024-01-01T00:00:11+00:00", drawdown=True))
    rolled = svc.evaluate_and_apply(state_store=store, settings=settings, now_utc=now)
    assert rolled is not None and rolled.outcome == "ROLLED_BACK"

    conn = sqlite3.connect(str(db))
    try:
        changes = conn.execute("SELECT COUNT(*) FROM stage7_param_changes").fetchone()[0]
    finally:
        conn.close()
    assert changes >= 3
