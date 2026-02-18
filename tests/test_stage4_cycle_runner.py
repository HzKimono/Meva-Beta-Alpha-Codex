from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import OrderSide, PairInfo
from btcbot.domain.stage4 import Order
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.state_store import StateStore


class FakeExchange:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_orderbook(self, symbol: str) -> tuple[float, float]:
        self.calls.append(f"orderbook:{symbol}")
        return (100.0, 102.0)

    def get_balances(self):
        self.calls.append("balances")
        return [type("B", (), {"asset": "TRY", "free": Decimal("100")})()]

    def list_open_orders(self, symbol: str):
        self.calls.append(f"open_orders:{symbol}")
        return []

    def get_recent_fills(self, symbol: str, since_ms: int | None = None):
        del since_ms
        self.calls.append(f"fills:{symbol}")
        return [
            TradeFill(
                fill_id=f"f-{symbol}",
                order_id=f"o-{symbol}",
                symbol=symbol,
                side=OrderSide.BUY,
                price=Decimal("100"),
                qty=Decimal("0.1"),
                fee=Decimal("0"),
                fee_currency="TRY",
                ts=datetime.now(UTC),
            )
        ]

    def get_exchange_info(self) -> list[PairInfo]:
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

    def submit_limit_order(self, symbol, side, price, qty, client_order_id):
        del symbol, side, price, qty, client_order_id
        self.calls.append("submit")
        return type("Ack", (), {"exchange_order_id": "ex-1", "status": "submitted"})()

    def cancel_order_by_exchange_id(self, exchange_order_id: str):
        del exchange_order_id
        self.calls.append("cancel")
        return True

    def cancel_order_by_client_order_id(self, client_order_id: str):
        del client_order_id
        return True

    def close(self) -> None:
        self.calls.append("close")


def test_runner_writes_audit_with_mandatory_counts(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    db_path = tmp_path / "runner.sqlite"
    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(db_path), SYMBOLS="BTC_TRY"
    )

    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(db_path))
    with store._connect() as conn:
        row = conn.execute(
            "SELECT counts_json, envelope_json FROM cycle_audit ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    counts = json.loads(row["counts_json"])
    envelope = json.loads(row["envelope_json"])
    for key in (
        "exchange_open",
        "db_open",
        "imported",
        "enriched",
        "unknown_closed",
        "external_missing_client_id",
        "fills_fetched",
        "fills_applied",
        "planned_actions",
        "accepted_actions",
        "executed",
        "submitted",
        "canceled",
        "rejected_min_notional",
        "accepted_by_risk",
        "rejected_by_risk",
    ):
        assert key in counts
    assert counts["accepted_by_risk"] == counts["planned_actions"]
    assert counts["rejected_by_risk"] == 0
    assert envelope["command"] == "stage4-run"
    assert envelope["symbols"] == ["BTCTRY"]


def test_runner_per_symbol_failure_is_non_fatal(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()

    def flaky_open_orders(symbol: str):
        if symbol == "ETHTRY":
            raise RuntimeError("boom")
        return []

    exchange.list_open_orders = flaky_open_orders  # type: ignore[assignment]
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "partial.sqlite"),
        SYMBOLS="BTC_TRY,ETH_TRY",
    )
    assert runner.run_one_cycle(settings) == 0


def test_runner_order_of_stage4_pipeline(monkeypatch, tmp_path) -> None:
    order: list[str] = []
    runner = Stage4CycleRunner()
    exchange = FakeExchange()

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    from btcbot.services import stage4_cycle_runner as module

    class FakeReconcileService:
        def resolve(self, exchange_open_orders, db_open_orders):
            del exchange_open_orders, db_open_orders
            order.append("reconcile")
            return type(
                "R",
                (),
                {
                    "import_external": [],
                    "enrich_exchange_ids": [],
                    "mark_unknown_closed": [],
                    "external_missing_client_id": [],
                },
            )()

    class FakeAccountingService(module.AccountingService):
        def fetch_new_fills(self, symbol: str):
            del symbol
            order.append("accounting.fetch")
            return type("FF", (), {"fills": [], "cursor_after": None})()

        def apply_fills(self, fills, *, mark_prices, try_cash):
            del fills, mark_prices, try_cash
            order.append("accounting.apply")
            return super().apply_fills([], mark_prices={}, try_cash=Decimal("0"))

    class FakeLifecycleService:
        def __init__(self, stale_after_sec: int) -> None:
            del stale_after_sec

        def plan(self, intents, current_open_orders, mid_price):
            del intents, current_open_orders, mid_price
            order.append("lifecycle")
            return type("P", (), {"actions": [], "audit_reasons": []})()

    class FakeRisk:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter_actions(self, actions, **kwargs):
            del kwargs
            order.append("risk")
            return actions, []

    class FakeExecution:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def execute_with_report(self, actions):
            del actions
            order.append("execution")
            return type(
                "ER",
                (),
                {
                    "executed_total": 0,
                    "submitted": 0,
                    "canceled": 0,
                    "simulated": 0,
                    "rejected": 0,
                    "rejected_min_notional": 0,
                },
            )()

    monkeypatch.setattr(module, "ReconcileService", FakeReconcileService)
    monkeypatch.setattr(module, "AccountingService", FakeAccountingService)
    monkeypatch.setattr(module, "OrderLifecycleService", FakeLifecycleService)
    monkeypatch.setattr(module, "RiskPolicy", FakeRisk)
    monkeypatch.setattr(module, "ExecutionService", FakeExecution)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(tmp_path / "ord.sqlite"))
    assert runner.run_one_cycle(settings) == 0
    assert order[0] == "reconcile"
    fetch_count = order.count("accounting.fetch")
    assert fetch_count == len(settings.symbols)
    first_fetch_idx = order.index("accounting.fetch")
    last_fetch_idx = len(order) - 1 - order[::-1].index("accounting.fetch")
    apply_idx = order.index("accounting.apply")
    lifecycle_idx = order.index("lifecycle")
    risk_idx = order.index("risk")
    execution_idx = order.index("execution")

    assert first_fetch_idx > 0
    assert last_fetch_idx < apply_idx < lifecycle_idx < risk_idx < execution_idx


def test_no_fill_history_does_not_warn_or_mark_cursor_stall(monkeypatch, tmp_path, caplog) -> None:
    class NoFillsExchange(FakeExchange):
        def get_recent_fills(self, symbol: str, since_ms: int | None = None):
            del symbol, since_ms
            return []

    runner = Stage4CycleRunner()
    exchange = NoFillsExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    settings = Settings(
        DRY_RUN=False,
        LIVE_TRADING=False,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "no_fills.sqlite"),
        SYMBOLS="XRP_TRY",
        CURSOR_STALL_CYCLES=1,
    )

    with caplog.at_level(logging.WARNING):
        assert runner.run_one_cycle(settings) == 0

    assert "stage4_fills_fetch_failed" not in caplog.text

    store = StateStore(settings.state_db_path)
    with store._connect() as conn:
        rows = conn.execute("SELECT code FROM anomaly_events").fetchall()
    codes = {str(row["code"]) for row in rows}
    assert "CURSOR_STALL" not in codes


def test_runner_uses_normalized_cursor_keys(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "cursor.sqlite"),
        SYMBOLS="btc_try,BTC_TRY",
    )
    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(tmp_path / "cursor.sqlite"))
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT key FROM cursors WHERE key LIKE 'fills_cursor:%' ORDER BY key"
        ).fetchall()
    assert [row["key"] for row in rows] == ["fills_cursor:BTCTRY"]


def test_cursor_advances_when_new_fills_arrive(monkeypatch, tmp_path) -> None:
    class AdvancingExchange(FakeExchange):
        def __init__(self) -> None:
            super().__init__()
            self._seen = False

        def get_recent_fills(self, symbol: str, since_ms: int | None = None):
            del since_ms
            if self._seen:
                return []
            self._seen = True
            return super().get_recent_fills(symbol)

    runner = Stage4CycleRunner()
    exchange = AdvancingExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    db_path = tmp_path / "cursor_adv.sqlite"
    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(db_path), SYMBOLS="BTC_TRY"
    )
    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(db_path))
    before = store.get_cursor("fills_cursor:BTCTRY")
    assert before is not None

    assert runner.run_one_cycle(settings) == 0
    after = store.get_cursor("fills_cursor:BTCTRY")
    assert after == before

    degrade = store.get_degrade_state_current()
    payload = json.loads(degrade.get("cursor_stall_cycles_json") or "{}")
    assert payload == {}


def test_stage4_cycle_records_snapshot_and_no_submit_in_killswitch(
    monkeypatch, tmp_path, caplog
) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=True,
        STATE_DB_PATH=str(tmp_path / "snap.sqlite"),
        SYMBOLS="BTC_TRY,ETH_TRY,SOL_TRY",
        TRY_CASH_TARGET="300",
    )

    with caplog.at_level(logging.INFO):
        assert runner.run_one_cycle(settings) == 0

    store = StateStore(settings.state_db_path)
    with store._connect() as conn:
        row = conn.execute("SELECT cycle_id FROM account_snapshots LIMIT 1").fetchone()
    assert row is not None
    assert "stage4_account_snapshot" in caplog.text
    assert "stage4_allocation_plan" in caplog.text
    assert "submit" not in exchange.calls


def test_bootstrap_intents_respect_min_notional_threshold() -> None:
    runner = Stage4CycleRunner()
    pair = PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=6,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        tickSize=Decimal("0.1"),
        stepSize=Decimal("0.0001"),
    )

    intents, drop_reasons = runner._build_intents(
        cycle_id="cycle-1",
        symbols=["BTCTRY"],
        mark_prices={"BTCTRY": Decimal("100")},
        try_cash=Decimal("200"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=[pair],
        min_order_notional_try=Decimal("200"),
        bootstrap_notional_try=Decimal("200"),
        max_notional_per_order_try=Decimal("200"),
    )

    assert len(intents) == 1
    assert intents[0].price * intents[0].qty >= Decimal("200")
    assert drop_reasons == {}


def test_bootstrap_intents_skip_when_budget_below_min_notional() -> None:
    runner = Stage4CycleRunner()
    pair = PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=6,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        tickSize=Decimal("0.1"),
        stepSize=Decimal("0.0001"),
    )

    intents, drop_reasons = runner._build_intents(
        cycle_id="cycle-1",
        symbols=["BTCTRY"],
        mark_prices={"BTCTRY": Decimal("100")},
        try_cash=Decimal("200"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=[pair],
        min_order_notional_try=Decimal("200"),
        bootstrap_notional_try=Decimal("50"),
        max_notional_per_order_try=Decimal("50"),
    )

    assert intents == []
    assert drop_reasons.get("bootstrap_budget_below_min_notional") == 1


def test_bootstrap_intents_skip_when_open_buy_order_exists() -> None:
    runner = Stage4CycleRunner()
    pair = PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=6,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        tickSize=Decimal("0.1"),
        stepSize=Decimal("0.0001"),
    )
    now = datetime.now(UTC)
    open_buy = Order(
        symbol="BTCTRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now,
        updated_at=now,
        client_order_id="cid-open-buy",
        mode="live",
    )

    intents, drop_reasons = runner._build_intents(
        cycle_id="cycle-1",
        symbols=["BTCTRY"],
        mark_prices={"BTCTRY": Decimal("100")},
        try_cash=Decimal("500"),
        open_orders=[open_buy],
        live_mode=True,
        bootstrap_enabled=True,
        pair_info=[pair],
        min_order_notional_try=Decimal("10"),
        bootstrap_notional_try=Decimal("200"),
        max_notional_per_order_try=Decimal("200"),
    )

    assert intents == []
    assert drop_reasons.get("skipped_due_to_open_orders") == 1
