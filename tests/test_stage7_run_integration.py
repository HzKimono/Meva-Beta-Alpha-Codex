from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from btcbot import cli
from btcbot.config import Settings
from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.domain.risk_budget import Mode
from btcbot.services.state_store import StateStore


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


def test_stage7_run_respects_reduce_risk_mode(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_reduce.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Exchange:
        def get_orderbook(self, symbol):
            del symbol
            return Decimal("100"), Decimal("102")

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: _Exchange(),
    )
    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.get_latest_risk_mode",
        lambda self: Mode.REDUCE_RISK_ONLY,
    )
    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.list_stage4_open_orders",
        lambda self: [
            SimpleNamespace(
                status="simulated_submitted",
                symbol="BTC_TRY",
                side="BUY",
                price=Decimal("101"),
                qty=Decimal("1"),
                client_order_id="b1",
                exchange_order_id="e1",
            ),
            SimpleNamespace(
                status="simulated_submitted",
                symbol="BTC_TRY",
                side="SELL",
                price=Decimal("101"),
                qty=Decimal("1"),
                client_order_id="s1",
                exchange_order_id="e2",
            ),
        ],
    )

    store = StateStore(db_path=str(db_path))
    store.append_ledger_events(
        [
            LedgerEvent(
                event_id="seed-buy",
                ts=datetime(2024, 1, 1, tzinfo=UTC),
                symbol="BTCTRY",
                type=LedgerEventType.FILL,
                side="BUY",
                qty=Decimal("1"),
                price=Decimal("100"),
                fee=None,
                fee_currency=None,
                exchange_trade_id="seed-buy",
                exchange_order_id="seed-order",
                client_order_id="seed-client",
                meta={"source": "test"},
            )
        ]
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
    finally:
        conn.close()

    assert cycle is not None
    mode_payload = json.loads(str(cycle["mode_json"]))
    decisions = json.loads(str(cycle["order_decisions_json"]))
    order = {"NORMAL": 0, "REDUCE_RISK_ONLY": 1, "OBSERVE_ONLY": 2}
    assert mode_payload["base_mode"] == "REDUCE_RISK_ONLY"
    assert order[mode_payload["final_mode"]] >= order[mode_payload["base_mode"]]
    assert any(d.get("status") == "submitted" and d.get("side") == "SELL" for d in decisions)
    assert any(d.get("status") == "skipped" for d in decisions)
