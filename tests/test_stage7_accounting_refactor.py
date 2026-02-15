from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from btcbot import cli
from btcbot.config import Settings
from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, PnLSnapshot
from btcbot.services.anomaly_detector_service import AnomalyDetectorService
from btcbot.services.ledger_service import LedgerService, PnlReport
from btcbot.services.state_store import StateStore


def test_equity_breakdown_uses_cash_plus_mtm(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "eq.db"))
    svc = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))
    event = LedgerEvent(
        event_id="fill-1",
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="BTCTRY",
        type=LedgerEventType.FILL,
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        fee=None,
        fee_currency=None,
        exchange_trade_id="1",
        exchange_order_id="o1",
        client_order_id="c1",
        meta={},
    )
    store.append_ledger_events([event])
    snap = svc.snapshot(mark_prices={"BTCTRY": Decimal("120")}, cash_try=Decimal("300"))
    assert snap.position_mtm_try == Decimal("120")
    assert snap.equity_try == Decimal("420")
    assert snap.net_pnl_try == Decimal("20")


def test_drawdown_seed_zero_on_first_point(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "dd.db"))
    svc = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))
    now = datetime(2024, 1, 1, tzinfo=UTC)
    snap = svc.snapshot(mark_prices={}, cash_try=Decimal("500"), ts=now)
    assert snap.max_drawdown == Decimal("0")


def test_pnl_divergence_details_are_structured() -> None:
    detector = AnomalyDetectorService()
    events = detector.detect(
        market_data_age_seconds={},
        reject_count=0,
        cycle_duration_ms=1,
        cursor_stall_by_symbol={},
        pnl_snapshot=PnLSnapshot(
            total_equity_try=Decimal("100"),
            realized_today_try=Decimal("0"),
            drawdown_pct=Decimal("0"),
            ts=datetime.now(UTC),
            realized_total_try=Decimal("0"),
        ),
        pnl_report=PnlReport(
            realized_pnl_total=Decimal("1"),
            unrealized_pnl_total=Decimal("2"),
            fees_total_by_currency={"TRY": Decimal("3")},
            per_symbol=[],
            equity_estimate=Decimal("0"),
        ),
    )
    pnl = [e for e in events if e.code.value == "PNL_DIVERGENCE"][0]
    assert "recomputed_equity_try" in pnl.details
    assert "fees_try" in pnl.details


def test_settings_load_stage7_env(monkeypatch) -> None:
    monkeypatch.setenv("TRY_CASH_TARGET", "777")
    monkeypatch.setenv("TRY_CASH_MAX", "999")
    monkeypatch.setenv("MAX_ORDERS_PER_CYCLE", "8")
    monkeypatch.setenv("NOTIONAL_CAP_TRY_PER_CYCLE", "5555")
    monkeypatch.setenv("STAGE7_ORDER_OFFSET_BPS", "9")
    monkeypatch.setenv("STAGE7_MAX_DRAWDOWN_PCT", "0.90")
    s = Settings()
    assert s.try_cash_target == Decimal("777")
    assert s.try_cash_max == Decimal("999")
    assert s.max_orders_per_cycle == 8
    assert s.notional_cap_try_per_cycle == Decimal("5555")
    assert s.stage7_order_offset_bps == Decimal("9")
    assert s.stage7_max_drawdown_pct == Decimal("0.90")


def test_stage7_five_cycles_drawdown_and_reject_metrics(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "s7_5.db"

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle",
        lambda self, settings: 0,
    )

    class _Exchange:
        def get_exchange_info(self):
            return [SimpleNamespace(pair_symbol="BTC_TRY")]

        def get_ticker_stats(self):
            return [{"pairSymbol": "BTC_TRY", "volume": "1000", "last": "100", "ts": 1704067200}]

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "100"} for _ in range(lookback)]

        def get_orderbook(self, symbol):
            del symbol
            return Decimal("99"), Decimal("101")

        def get_balances(self):
            return [SimpleNamespace(asset="TRY", free=Decimal("300"), locked=Decimal("0"))]

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        lambda *args, **kwargs: SimpleNamespace(
            selected_symbols=["BTCTRY"],
            scored=[SimpleNamespace(symbol="BTCTRY", total_score=Decimal("1"), breakdown={})],
        ),
    )

    monkeypatch.setattr(
        "btcbot.services.stage7_risk_budget_service.Stage7RiskBudgetService.decide",
        lambda self, settings, now_utc, inputs, previous_decision: SimpleNamespace(
            mode=__import__("btcbot.domain.risk_models", fromlist=["RiskMode"]).RiskMode.NORMAL,
            reasons={"rules": [{"code": "ok"}]},
            cooldown_until=None,
            decided_at=now_utc,
            inputs_hash="ok",
        ),
    )

    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    def _fake_process(self, **kwargs):
        del self, kwargs
        orders = [
            SimpleNamespace(
                status=_Status("REJECTED"),
                client_order_id=f"r{i}",
                symbol="BTCTRY",
                side="BUY",
                qty=Decimal("1"),
                avg_fill_price_try=None,
                filled_qty=Decimal("0"),
                order_id=f"o{i}",
            )
            for i in range(3)
        ]
        return orders, []

    monkeypatch.setattr("btcbot.services.oms_service.OMSService.process_intents", _fake_process)

    store = StateStore(db_path=str(db_path))
    store.save_stage4_pnl_snapshot(
        PnLSnapshot(
            total_equity_try=Decimal("1000"),
            realized_today_try=Decimal("0"),
            drawdown_pct=Decimal("0"),
            ts=datetime(2024, 1, 1, tzinfo=UTC),
            realized_total_try=Decimal("0"),
        )
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE7_MAX_DRAWDOWN_PCT=Decimal("0.90"),
        STAGE7_REJECT_SPIKE_THRESHOLD=3,
    )

    for i in range(5):
        runner = __import__(
            "btcbot.services.stage7_cycle_runner", fromlist=["Stage7CycleRunner"]
        ).Stage7CycleRunner()
        assert (
            runner.run_one_cycle(
                settings,
                now_utc=datetime(2024, 1, 2, tzinfo=UTC) + timedelta(minutes=i),
                cycle_id=f"c{i}",
                run_id=f"r{i}",
                stage4_result=0,
                enable_adaptation=False,
                use_active_params=False,
            )
            == 0
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM stage7_run_metrics ORDER BY ts").fetchall()
        pnl_div = conn.execute(
            "SELECT COUNT(*) FROM anomaly_events WHERE code='PNL_DIVERGENCE'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert len(rows) == 5
    assert pnl_div == 0
    import json

    for row in rows:
        rejected = int(row["oms_rejected_count"])
        alerts = json.loads(str(row["alert_flags_json"]))
        assert rejected >= 3
        assert alerts["reject_spike"] == (rejected >= 3)
        assert row["max_drawdown_ratio"] == "0.7"

    settings_strict = settings.model_copy(update={"stage7_max_drawdown_pct": Decimal("0.60")})
    assert cli.run_cycle_stage7(settings_strict, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            "SELECT alert_flags_json FROM stage7_run_metrics ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    import json

    alerts = json.loads(str(latest["alert_flags_json"]))
    assert alerts["drawdown_breach"] is True
