from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from btcbot import cli
from btcbot.config import Settings
from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.domain.risk_models import RiskDecision, RiskMode


def _fake_stage4(self, settings):
    del self, settings
    return 0


class _Pair:
    def __init__(self, pair_symbol: str) -> None:
        self.pair_symbol = pair_symbol


class _Exchange:
    def get_exchange_info(self):
        return [_Pair("BTC_TRY")]

    def get_ticker_stats(self):
        return [{"pairSymbol": "BTC_TRY", "volume": "1000", "last": "100"}]

    def get_candles(self, symbol, lookback):
        del symbol
        return [{"close": "100"} for _ in range(lookback)]

    def get_orderbook(self, symbol):
        del symbol
        return Decimal("99"), Decimal("101")

    def close(self):
        return None


def _selected_universe(*args, **kwargs):
    del args, kwargs
    return SimpleNamespace(
        selected_symbols=["BTCTRY"],
        scored=[SimpleNamespace(symbol="BTCTRY", total_score=Decimal("1"), breakdown={})],
    )


def test_stage7_observe_only_risk_decision_skips_intents_and_persists_trace(monkeypatch, tmp_path):
    db_path = tmp_path / "s7_observe_risk.db"
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: _Exchange(),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_universe,
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_risk_budget_service.Stage7RiskBudgetService.decide",
        lambda self, settings, now_utc, inputs, previous_decision: RiskDecision(
            mode=RiskMode.OBSERVE_ONLY,
            reasons={"rules": [{"code": "forced_observe"}]},
            cooldown_until=now_utc,
            decided_at=now_utc,
            inputs_hash="forced",
        ),
    )

    settings = Settings(
        DRY_RUN=True, STAGE7_ENABLED=True, STATE_DB_PATH=str(db_path), SYMBOLS="BTC_TRY"
    )
    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute("SELECT mode_json FROM stage7_cycle_trace").fetchone()
        intents_count = conn.execute("SELECT COUNT(*) FROM stage7_order_intents").fetchone()[0]
        risk_row = conn.execute("SELECT mode, reasons_json FROM stage7_risk_decisions").fetchone()
    finally:
        conn.close()

    payload = json.loads(str(cycle["mode_json"]))
    assert payload["risk_mode"] == "OBSERVE_ONLY"
    assert payload["final_mode"] == "OBSERVE_ONLY"
    assert "risk_reasons" in payload
    assert intents_count == 0
    assert risk_row["mode"] == "OBSERVE_ONLY"


def test_stage7_reduce_risk_decision_suppresses_buy_and_keeps_sell(monkeypatch, tmp_path):
    db_path = tmp_path / "s7_reduce_risk.db"
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: _Exchange(),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_universe,
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_risk_budget_service.Stage7RiskBudgetService.decide",
        lambda self, settings, now_utc, inputs, previous_decision: RiskDecision(
            mode=RiskMode.REDUCE_RISK_ONLY,
            reasons={"rules": [{"code": "forced_reduce"}]},
            cooldown_until=None,
            decided_at=now_utc,
            inputs_hash="forced",
        ),
    )

    from btcbot.services.state_store import StateStore

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
        STAGE7_RULES_REQUIRE_METADATA=False,
    )
    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute(
            "SELECT mode_json, portfolio_plan_json FROM stage7_cycle_trace"
        ).fetchone()
    finally:
        conn.close()

    payload = json.loads(str(cycle["mode_json"]))
    plan = json.loads(str(cycle["portfolio_plan_json"]))
    assert payload["risk_mode"] == "REDUCE_RISK_ONLY"
    assert payload["final_mode"] in {"REDUCE_RISK_ONLY", "OBSERVE_ONLY"}
    assert all(action.get("side") == "SELL" for action in plan.get("actions", []))
