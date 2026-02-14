from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

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


def test_seed_apply_and_rollback_marking(tmp_path) -> None:
    db = tmp_path / "adapt.db"
    store = StateStore(str(db))
    settings = Settings(
        STATE_DB_PATH=str(db),
        STAGE7_ENABLED=True,
        DRY_RUN=True,
        NOTIONAL_CAP_TRY_PER_CYCLE="1000",
    )
    svc = AdaptationService()
    now = datetime(2024, 1, 1, tzinfo=UTC)

    seeded = store.get_active_stage7_params(settings=settings, now_utc=now)
    assert seeded.version == 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        seed_active = conn.execute(
            "SELECT * FROM stage7_params_active WHERE key='active'"
        ).fetchone()
        seed_ckpt = conn.execute(
            "SELECT * FROM stage7_params_checkpoints WHERE version=1"
        ).fetchone()
    finally:
        conn.close()
    assert seed_active is not None
    assert seed_ckpt is not None
    assert int(seed_ckpt["is_good"]) == 1

    for i in range(3):
        store.save_stage7_run_metrics(f"c{i}", _metric(f"2024-01-01T00:00:0{i}+00:00"))
    applied = svc.evaluate_and_apply(state_store=store, settings=settings, now_utc=now)
    assert applied is not None
    assert applied.outcome == "APPLIED"

    active_v2 = store.get_active_stage7_params(settings=settings, now_utc=now)
    assert active_v2.version == 2

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        applied_change = conn.execute(
            "SELECT * FROM stage7_param_changes WHERE outcome='APPLIED' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        v2_ckpt = conn.execute("SELECT * FROM stage7_params_checkpoints WHERE version=2").fetchone()
    finally:
        conn.close()
    assert applied_change is not None
    assert v2_ckpt is not None
    assert int(v2_ckpt["is_good"]) == 1

    store.save_stage7_run_metrics("cx", _metric("2024-01-01T00:01:00+00:00", mode="OBSERVE_ONLY"))
    rejected = svc.evaluate_and_apply(
        state_store=store, settings=settings, now_utc=now + timedelta(seconds=10)
    )
    assert rejected is not None
    assert rejected.outcome == "REJECTED"

    store.save_stage7_run_metrics("cy", _metric("2024-01-01T00:01:01+00:00", drawdown=True))
    rolled = svc.evaluate_and_apply(
        state_store=store, settings=settings, now_utc=now + timedelta(seconds=20)
    )
    assert rolled is not None
    assert rolled.outcome == "ROLLED_BACK"

    active_after_rollback = store.get_active_stage7_params(settings=settings, now_utc=now)
    assert active_after_rollback.version == 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        v2_ckpt_after = conn.execute(
            "SELECT * FROM stage7_params_checkpoints WHERE version=2"
        ).fetchone()
        rollback_change = conn.execute(
            """
            SELECT * FROM stage7_param_changes
            WHERE outcome='ROLLED_BACK'
            ORDER BY ts DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert v2_ckpt_after is not None
    assert int(v2_ckpt_after["is_good"]) == 0
    assert rollback_change is not None

    last_good = store.get_last_good_stage7_params_checkpoint()
    assert last_good is not None
    assert last_good.version == 1

    payload = json.loads(str(rollback_change["change_json"]))
    assert payload["outcome"] == "ROLLED_BACK"
