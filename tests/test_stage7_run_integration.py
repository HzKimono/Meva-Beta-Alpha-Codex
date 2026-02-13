from __future__ import annotations

import json
import sqlite3

from btcbot import cli
from btcbot.config import Settings


def test_stage7_run_dry_run_persists_trace_and_metrics(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute("SELECT * FROM stage7_cycle_trace").fetchone()
        metrics = conn.execute("SELECT * FROM stage7_ledger_metrics").fetchone()
    finally:
        conn.close()

    assert cycle is not None
    assert metrics is not None
    mode_payload = json.loads(str(cycle["mode_json"]))
    order = {"NORMAL": 0, "REDUCE_RISK_ONLY": 1, "OBSERVE_ONLY": 2}
    assert order[mode_payload["final_mode"]] >= order[mode_payload["base_mode"]]
