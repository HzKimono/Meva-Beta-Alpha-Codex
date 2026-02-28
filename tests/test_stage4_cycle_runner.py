from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

import httpx

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.config import Settings
from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import OrderSide, PairInfo
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, Order
from btcbot.services.risk_budget_service import RiskBudgetService
from btcbot.services.stage4_cycle_runner import MarketSnapshot, Stage4CycleRunner
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

    def health_snapshot(self) -> dict[str, object]:
        return {"degraded": False, "breaker_open": False}

    def close(self) -> None:
        self.calls.append("close")


class FreezeTriggerExchange(FakeExchange):
    def list_open_orders(self, symbol: str):
        self.calls.append(f"open_orders:{symbol}")
        return [
            Order(
                symbol=symbol,
                side="buy",
                type="limit",
                price=Decimal("100"),
                qty=Decimal("0.1"),
                status="open",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                exchange_order_id="ex-missing-client",
                client_order_id=None,
                mode="live",
            )
        ]




def test_runner_writes_stage4_run_metrics(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    db_path = tmp_path / "runner_stage4_metrics.sqlite"
    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(db_path), SYMBOLS="BTC_TRY"
    )

    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(db_path))
    with store._connect() as conn:
        row = conn.execute(
            "SELECT * FROM stage4_run_metrics ORDER BY ts DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    reasons = json.loads(str(row["reasons_no_action_json"]))
    rejects_by_code = json.loads(str(row["rejects_by_code_json"]))
    assert isinstance(reasons, list)
    assert int(row["intents_created"]) == 1
    assert int(row["intents_after_risk"]) == 1
    assert int(row["orders_submitted"]) == 0
    assert row["breaker_state"] == "closed"
    assert int(row["degraded_mode"]) == 0
    assert rejects_by_code == {}


def test_runner_writes_stage4_run_metrics_no_action_reasons(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    class RejectAllRisk:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter_actions(self, actions, **kwargs):
            del kwargs
            return [], []

    monkeypatch.setattr("btcbot.services.stage4_cycle_runner.RiskPolicy", RejectAllRisk)

    db_path = tmp_path / "runner_stage4_metrics_reasons.sqlite"
    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(db_path), SYMBOLS="BTC_TRY"
    )

    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(db_path))
    with store._connect() as conn:
        row = conn.execute(
            "SELECT * FROM stage4_run_metrics ORDER BY ts DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    reasons = set(json.loads(str(row["reasons_no_action_json"])))
    assert "ALL_INTENTS_REJECTED_BY_RISK" in reasons
    assert "NO_INTENTS_CREATED" not in reasons
    assert int(row["intents_created"]) == 1
    assert int(row["intents_after_risk"]) == 0
    assert int(row["orders_submitted"]) == 0





def test_runner_risk_audit_status_format_and_rejected_count(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    class RejectWithNonRejectSubstringReason:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter_actions(self, actions, **kwargs):
            del kwargs
            decisions = [
                type(
                    "Decision",
                    (),
                    {
                        "action": action,
                        "accepted": False,
                        "reason": "max_open_orders",
                    },
                )()
                for action in actions
            ]
            return [], decisions

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.RiskPolicy",
        RejectWithNonRejectSubstringReason,
    )

    db_path = tmp_path / "runner_stage4_risk_audit.sqlite"
    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(db_path), SYMBOLS="BTC_TRY"
    )

    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(db_path))
    with store._connect() as conn:
        row = conn.execute(
            "SELECT counts_json, decisions_json FROM cycle_audit ORDER BY ts DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    counts = json.loads(str(row["counts_json"]))
    decisions = json.loads(str(row["decisions_json"]))

    assert counts["accepted_by_risk"] == 0
    assert counts["rejected_by_risk"] == 1
    assert any(
        isinstance(entry, str)
        and entry.startswith("risk:")
        and entry.endswith(":rejected:max_open_orders")
        for entry in decisions
    )
def test_extract_rejects_by_code_normalizes_numeric_codes() -> None:
    runner = Stage4CycleRunner()
    rejects = runner._extract_rejects_by_code(
        {
            "rejected": 3,
            "rejected_min_notional": 2,
            "rejected_1123": 4,
            "alloc_rejected_code_1123": 1,
            "pipeline_rejected_code_4001": 5,
            "rejected_code_42": 6,
        }
    )
    assert rejects == {"1123": 5, "4001": 5, "42": 6}

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


def test_bootstrap_intents_clamp_budget_to_min_notional_when_cash_sufficient() -> None:
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
        max_notional_per_order_try=Decimal("0"),
    )

    assert len(intents) == 1
    assert intents[0].price * intents[0].qty >= Decimal("200")
    assert drop_reasons == {}


def test_bootstrap_intents_skip_when_cash_below_min_notional() -> None:
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
        try_cash=Decimal("150"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=[pair],
        min_order_notional_try=Decimal("200"),
        bootstrap_notional_try=Decimal("50"),
        max_notional_per_order_try=Decimal("0"),
    )

    assert intents == []
    assert drop_reasons.get("cash_below_min_notional") == 1


def test_bootstrap_intents_skip_when_max_notional_below_min_notional() -> None:
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
        try_cash=Decimal("500"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=[pair],
        min_order_notional_try=Decimal("200"),
        bootstrap_notional_try=Decimal("50"),
        max_notional_per_order_try=Decimal("150"),
    )

    assert intents == []
    assert drop_reasons.get("max_notional_below_min_notional") == 1


def test_bootstrap_intents_skip_when_bootstrap_notional_is_disabled() -> None:
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
        try_cash=Decimal("500"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=[pair],
        min_order_notional_try=Decimal("200"),
        bootstrap_notional_try=Decimal("0"),
        max_notional_per_order_try=Decimal("0"),
    )

    assert intents == []
    assert drop_reasons.get("bootstrap_disabled") == 1

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


def _read_latest_cycle_counts(db_path) -> dict[str, int]:
    store = StateStore(str(db_path))
    with store._connect() as conn:
        row = conn.execute(
            "SELECT counts_json FROM cycle_audit ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["counts_json"])


class _TimestampedExchange(FakeExchange):
    def __init__(self, *, observed_at: datetime) -> None:
        super().__init__()
        self._observed_at = observed_at

    def get_orderbook_with_timestamp(self, symbol: str):
        del symbol
        return (Decimal("100"), Decimal("102"), self._observed_at)


class _MissingTimestampExchange(FakeExchange):
    def get_orderbook_with_timestamp(self, symbol: str):
        del symbol
        return (Decimal("100"), Decimal("102"), None)


def test_market_snapshot_falls_back_when_timestamp_missing() -> None:
    runner = Stage4CycleRunner()
    now = datetime.now(UTC)

    snapshot = runner._resolve_market_snapshot(
        _MissingTimestampExchange(),
        ["BTC_TRY"],
        cycle_now=now,
    )

    assert snapshot.fetched_at_by_symbol["BTCTRY"] is not None
    assert snapshot.age_seconds_by_symbol["BTCTRY"] >= Decimal("0")
    assert snapshot.age_seconds_by_symbol["BTCTRY"] != Decimal("999999")


def test_stale_market_snapshot_blocks_symbol_execution(monkeypatch, tmp_path, caplog) -> None:
    exchange = _TimestampedExchange(observed_at=datetime.now(UTC) - timedelta(minutes=30))
    runner = Stage4CycleRunner()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "stale_market.sqlite"),
        SYMBOLS="BTC_TRY",
        STALE_MARKET_DATA_SECONDS=10,
        DYNAMIC_UNIVERSE_ENABLED=False,
    )
    with caplog.at_level(logging.WARNING):
        assert runner.run_one_cycle(settings) == 0
    counts = _read_latest_cycle_counts(tmp_path / "stale_market.sqlite")
    assert counts["accepted_actions"] == 0
    assert "stale_market_data_age_exceeded" in caplog.text


def test_fresh_market_snapshot_keeps_symbol_tradable(monkeypatch, tmp_path, caplog) -> None:
    exchange = _TimestampedExchange(observed_at=datetime.now(UTC))
    runner = Stage4CycleRunner()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "fresh_market.sqlite"),
        SYMBOLS="BTC_TRY",
        STALE_MARKET_DATA_SECONDS=10,
        DYNAMIC_UNIVERSE_ENABLED=False,
    )
    with caplog.at_level(logging.WARNING):
        assert runner.run_one_cycle(settings) == 0
    counts = _read_latest_cycle_counts(tmp_path / "fresh_market.sqlite")
    assert counts["accepted_actions"] >= 0
    assert "stale_market_data_age_exceeded" not in caplog.text


class _AdapterBackedExchange:
    def __init__(self, *, client: BtcturkHttpClient) -> None:
        self.client = client

    def get_orderbook(self, symbol: str):
        return self.client.get_orderbook(symbol)

    def get_balances(self):
        return [type("B", (), {"asset": "TRY", "free": Decimal("100")})()]

    def list_open_orders(self, symbol: str):
        del symbol
        return []

    def get_recent_fills(self, symbol: str, since_ms: int | None = None):
        del symbol, since_ms
        return []

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
        return type("Ack", (), {"exchange_order_id": "ex-1", "status": "submitted"})()

    def cancel_order_by_exchange_id(self, exchange_order_id: str):
        del exchange_order_id
        return True

    def cancel_order_by_client_order_id(self, client_order_id: str):
        del client_order_id
        return True

    def close(self) -> None:
        self.client.close()


def test_stage4_stale_data_blocks_with_btcturk_adapter_timestamp_cache(
    monkeypatch, tmp_path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "data": {"bids": [["100", "1"]], "asks": [["102", "1"]]}},
            request=request,
        )

    client = BtcturkHttpClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
        orderbook_cache_ttl_s=120.0,
    )
    fresh_ts = datetime.now(UTC)
    client._orderbook_cache[("BTCTRY", None)] = (
        monotonic() + 120.0,
        (Decimal("100"), Decimal("102")),
        fresh_ts,
    )
    first = client.get_orderbook_with_timestamp("BTC_TRY")
    second = client.get_orderbook_with_timestamp("BTC_TRY")
    assert first[2] == fresh_ts
    assert second[2] == fresh_ts

    stale_ts = datetime.now(UTC) - timedelta(minutes=45)
    client._orderbook_cache[("BTCTRY", None)] = (
        monotonic() + 120.0,
        (Decimal("100"), Decimal("102")),
        stale_ts,
    )
    exchange = _AdapterBackedExchange(client=client)
    runner = Stage4CycleRunner()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    db_path = tmp_path / "stale_adapter.sqlite"
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(db_path),
        PROCESS_ROLE="MONITOR",
        SYMBOLS="BTC_TRY",
        STALE_MARKET_DATA_SECONDS=10,
        DYNAMIC_UNIVERSE_ENABLED=False,
    )
    assert runner.run_one_cycle(settings) == 0
    counts = _read_latest_cycle_counts(db_path)
    assert counts["accepted_actions"] == 0


def test_stage4_cycle_duration_ms_uses_real_end_timestamps(monkeypatch, tmp_path) -> None:
    from time import sleep as thread_sleep

    exchange = FakeExchange()
    runner = Stage4CycleRunner()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    from btcbot.services import stage4_cycle_runner as module

    captured: list[int | None] = []

    class FakeAnomalyDetector:
        def __init__(self, config=None, now_provider=None) -> None:
            del config, now_provider

        def detect(self, **kwargs):
            captured.append(kwargs.get("cycle_duration_ms"))
            return []

    class SlowExecution(module.ExecutionService):
        def execute_with_report(self, actions):
            thread_sleep(0.05)
            return super().execute_with_report(actions)

    monkeypatch.setattr(module, "AnomalyDetectorService", FakeAnomalyDetector)
    monkeypatch.setattr(module, "ExecutionService", SlowExecution)

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "duration.sqlite"),
        SYMBOLS="BTC_TRY",
        DYNAMIC_UNIVERSE_ENABLED=False,
    )
    assert runner.run_one_cycle(settings) == 0
    assert len(captured) >= 2
    assert captured[0] is not None and captured[1] is not None
    assert int(captured[1]) >= int(captured[0])




def test_stage4_db_killswitch_toggle_blocks_and_restores_submits(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    from btcbot.services import stage4_cycle_runner as module

    captured: list[list[str]] = []

    class CaptureExecution(module.ExecutionService):
        def execute_with_report(self, actions):
            captured.append([str(action.action_type) for action in actions])
            return super().execute_with_report(actions)

    monkeypatch.setattr(module, "ExecutionService", CaptureExecution)

    db_path = tmp_path / "stage4_db_killswitch.sqlite"
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(db_path),
        PROCESS_ROLE="MONITOR",
        SYMBOLS="BTC_TRY",
    )

    store = StateStore(str(db_path))
    store.set_kill_switch("MONITOR", True, reason="ops_test", until_ts=None)

    assert runner.run_one_cycle(settings) == 0
    assert captured[-1] == []

    store.set_kill_switch("MONITOR", False, reason="ops_test_off", until_ts=None)

    assert runner.run_one_cycle(settings) == 0
    assert "submit" in captured[-1]

def test_stage4_dry_run_never_submits_or_cancels(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "stage4_dryrun_nowrites.sqlite"),
        SYMBOLS="BTC_TRY",
    )

    assert runner.run_one_cycle(settings) == 0
    assert "submit" not in exchange.calls
    assert "cancel" not in exchange.calls


def test_stage4_cycle_applies_risk_policy_filters_actions_before_execution(
    monkeypatch, tmp_path
) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    from btcbot.services import stage4_cycle_runner as module

    class FilterOneRisk:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter_actions(self, actions, **kwargs):
            del kwargs
            accepted = [a for a in actions if (a.client_order_id or "") != "cid-filtered"]
            decisions = []
            for action in actions:
                accepted_flag = action in accepted
                reason = "accepted" if accepted_flag else "max_order_notional_try"
                decisions.append(type("D", (), {"action": action, "accepted": accepted_flag, "reason": reason})())
            return accepted, decisions

    captured: dict[str, list[str]] = {"executed": []}

    class CaptureExecution(module.ExecutionService):
        def execute_with_report(self, actions):
            captured["executed"] = [a.client_order_id or "" for a in actions]
            return super().execute_with_report(actions)

    class TwoActionLifecycle:
        def __init__(self, stale_after_sec: int) -> None:
            del stale_after_sec

        def plan(self, intents, current_open_orders, mid_price):
            del intents, current_open_orders, mid_price
            return type(
                "P",
                (),
                {
                    "actions": [
                        module.LifecycleAction(
                            action_type=module.LifecycleActionType.SUBMIT,
                            symbol="BTC_TRY",
                            side="buy",
                            price=Decimal("100"),
                            qty=Decimal("1"),
                            reason="test",
                            client_order_id="cid-accepted",
                        ),
                        module.LifecycleAction(
                            action_type=module.LifecycleActionType.SUBMIT,
                            symbol="BTC_TRY",
                            side="buy",
                            price=Decimal("100"),
                            qty=Decimal("1"),
                            reason="test",
                            client_order_id="cid-filtered",
                        ),
                    ],
                    "audit_reasons": [],
                },
            )()

    monkeypatch.setattr(module, "RiskPolicy", FilterOneRisk)
    monkeypatch.setattr(module, "ExecutionService", CaptureExecution)
    monkeypatch.setattr(module, "OrderLifecycleService", TwoActionLifecycle)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(tmp_path / "filter.sqlite"), SYMBOLS="BTC_TRY")

    assert runner.run_one_cycle(settings) == 0
    assert captured["executed"] == ["cid-accepted"]


def test_runner_unknown_freeze_triggers_and_persists(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FreezeTriggerExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    db_path = tmp_path / "runner_stage4_freeze.db"
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE4_UNKNOWN_FREEZE_ENABLED=True,
        PROCESS_ROLE="MONITOR",
    )

    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(db_path))
    freeze = store.stage4_get_freeze("MONITOR")
    assert freeze.active is True
    assert freeze.reason == "external_missing_client_id"


def test_runner_unknown_freeze_suppresses_submits(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    db_path = tmp_path / "runner_stage4_freeze_suppress.db"
    store = StateStore(str(db_path))
    store.stage4_set_freeze("MONITOR", reason="unknown_open_orders", details={"count": 1})

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE4_UNKNOWN_FREEZE_ENABLED=True,
        PROCESS_ROLE="MONITOR",
    )

    assert runner.run_one_cycle(settings) == 0

    with store._connect() as conn:
        row = conn.execute(
            "SELECT counts_json FROM cycle_audit ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    counts = json.loads(str(row["counts_json"]))
    assert counts["freeze_active"] == 1
    assert counts["freeze_suppressed_submit"] >= 1


def test_build_rejects_breakdown_prefers_execution_reasons() -> None:
    runner = Stage4CycleRunner()
    execution_report = type(
        "ExecutionReport",
        (),
        {
            "rejects_breakdown": {"min_total": 2},
            "reject_details": (
                {
                    "reason": "min_total",
                    "rejected_by_code": "unknown",
                    "q_price": "100.00",
                    "q_qty": "0.9999",
                    "total_try": "99.990000",
                },
            ),
            "rejected": 2,
        },
    )()

    breakdown = runner._build_rejects_breakdown({"rejected_min_notional": 2}, execution_report)
    assert breakdown == {"by_reason": {"min_total": 2}}


def test_summary_reject_context_exposes_min_notional_fields() -> None:
    runner = Stage4CycleRunner()
    execution_report = type(
        "ExecutionReport",
        (),
        {
            "reject_details": (
                {
                    "reason": "min_total",
                    "rejected_by_code": "unknown",
                    "symbol": "BTC_TRY",
                    "side": "buy",
                    "q_price": "100.00",
                    "q_qty": "0.9999",
                    "total_try": "99.990000",
                    "min_required_settings": "100",
                    "min_required_exchange_rule": "100",
                },
            )
        },
    )()

    context = runner._summary_reject_context(execution_report)
    assert context["reason"] == "min_total"
    assert context["q_price"] == "100.00"
    assert context["min_required_settings"] == "100"
    assert context["min_required_exchange_rule"] == "100"




def _prefilter_pair_info(min_total: str = "120") -> list[PairInfo]:
    return [
        PairInfo(
            pairSymbol="BTCTRY",
            numeratorScale=4,
            denominatorScale=2,
            minTotalAmount=Decimal(min_total),
            tickSize=Decimal("0.01"),
            stepSize=Decimal("0.0001"),
        )
    ]


def _submit_action(*, price: str, qty: str, cid: str) -> LifecycleAction:
    return LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal(price),
        qty=Decimal(qty),
        reason="test",
        client_order_id=cid,
    )
def test_prefilter_min_notional_rescues_floor_rounding_gap(caplog) -> None:
    runner = Stage4CycleRunner()
    action = _submit_action(price="100.005", qty="1.19995", cid="cid-rescue")

    with caplog.at_level(logging.INFO):
        filtered, dropped = runner._prefilter_submit_actions_min_notional(
            actions=[action],
            pair_info=_prefilter_pair_info(),
            min_order_notional_try=Decimal("120"),
            cycle_id="cycle-1",
        )

    assert dropped == 0
    assert len(filtered) == 1
    rescued = filtered[0]
    assert rescued.qty >= action.qty
    assert rescued.price * rescued.qty >= Decimal("120")
    assert all(item.price * item.qty >= Decimal("120") for item in filtered)

    rescue_records = [r for r in caplog.records if r.msg == "stage4_prefilter_min_notional_rescue"]
    assert len(rescue_records) == 1
    rescue_extra = rescue_records[0].extra
    assert {"symbol", "side", "min_required", "before_notional", "after_notional"}.issubset(
        rescue_extra
    )


def test_prefilter_min_notional_drops_when_intent_below_minimum(caplog) -> None:
    runner = Stage4CycleRunner()
    action = _submit_action(price="100", qty="1.19995", cid="cid-drop")

    with caplog.at_level(logging.INFO):
        filtered, dropped = runner._prefilter_submit_actions_min_notional(
            actions=[action],
            pair_info=_prefilter_pair_info(),
            min_order_notional_try=Decimal("120"),
            cycle_id="cycle-1",
        )

    assert filtered == []
    assert dropped == 1
    drop_records = [r for r in caplog.records if r.msg == "stage4_prefilter_drop_min_notional"]
    assert len(drop_records) == 1
    drop_extra = drop_records[0].extra
    assert {"symbol", "required_min_notional_try", "reason_code"}.issubset(drop_extra)




def test_resolve_action_intent_notional_precedence() -> None:
    runner = Stage4CycleRunner()
    action = _submit_action(price="10", qty="5", cid="cid-resolve")

    assert runner._resolve_action_intent_notional(action) is None
    object.__setattr__(action, "target_notional_try", Decimal("70"))
    assert runner._resolve_action_intent_notional(action) == Decimal("70")
    object.__setattr__(action, "notional_try", Decimal("80"))
    assert runner._resolve_action_intent_notional(action) == Decimal("80")
    object.__setattr__(action, "intent_notional_try", Decimal("90"))
    assert runner._resolve_action_intent_notional(action) == Decimal("90")

def test_prefilter_min_notional_intent_resolution_precedence() -> None:
    runner = Stage4CycleRunner()

    with_intent = _submit_action(price="100", qty="1.1", cid="cid-intent")
    object.__setattr__(with_intent, "target_notional_try", Decimal("200"))
    object.__setattr__(with_intent, "notional_try", Decimal("200"))
    object.__setattr__(with_intent, "intent_notional_try", Decimal("110"))

    with_notional = _submit_action(price="100", qty="1.1", cid="cid-notional")
    object.__setattr__(with_notional, "target_notional_try", Decimal("200"))
    object.__setattr__(with_notional, "notional_try", Decimal("120"))

    with_target = _submit_action(price="100", qty="1.1", cid="cid-target")
    object.__setattr__(with_target, "target_notional_try", Decimal("120"))

    fallback = _submit_action(price="100.005", qty="1.19995", cid="cid-fallback")

    filtered, dropped = runner._prefilter_submit_actions_min_notional(
        actions=[with_intent, with_notional, with_target, fallback],
        pair_info=_prefilter_pair_info(),
        min_order_notional_try=Decimal("120"),
        cycle_id="cycle-1",
    )

    assert dropped == 1
    ids = {item.client_order_id for item in filtered}
    assert ids == {"cid-notional", "cid-target", "cid-fallback"}


def test_prefilter_min_notional_drops_safely_when_quantized_price_is_zero() -> None:
    runner = Stage4CycleRunner()
    zero_price_action = _submit_action(price="0.009", qty="20000", cid="cid-zero-price")

    filtered, dropped = runner._prefilter_submit_actions_min_notional(
        actions=[zero_price_action],
        pair_info=_prefilter_pair_info(),
        min_order_notional_try=Decimal("120"),
        cycle_id="cycle-1",
    )

    assert filtered == []
    assert dropped == 1


def test_prefilter_min_notional_is_deterministic_across_calls() -> None:
    runner = Stage4CycleRunner()
    action = _submit_action(price="100.005", qty="1.19995", cid="cid-deterministic")

    first_filtered, first_dropped = runner._prefilter_submit_actions_min_notional(
        actions=[action],
        pair_info=_prefilter_pair_info(),
        min_order_notional_try=Decimal("120"),
        cycle_id="cycle-1",
    )
    second_filtered, second_dropped = runner._prefilter_submit_actions_min_notional(
        actions=[action],
        pair_info=_prefilter_pair_info(),
        min_order_notional_try=Decimal("120"),
        cycle_id="cycle-1",
    )

    assert first_dropped == second_dropped == 0
    assert first_filtered == second_filtered


def test_stage4_risk_budget_not_fail_closed_when_some_symbols_missing_marks(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()
    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    cycle_now = datetime(2025, 1, 1, tzinfo=UTC)

    def _snapshot(self, *args, **kwargs):
        del self, args, kwargs
        return MarketSnapshot(
            mark_prices={"BTCTRY": Decimal("100")},
            orderbooks={"BTCTRY": (Decimal("99"), Decimal("101"))},
            anomalies=set(),
            spreads_bps={"BTCTRY": Decimal("10")},
            age_seconds_by_symbol={"BTCTRY": Decimal("0"), "ETHTRY": Decimal("0")},
            fetched_at_by_symbol={"BTCTRY": cycle_now, "ETHTRY": cycle_now},
            max_data_age_seconds=Decimal("0"),
        )

    monkeypatch.setattr(Stage4CycleRunner, "_resolve_market_snapshot", _snapshot)

    captured: dict[str, object] = {}
    original_compute = RiskBudgetService.compute_decision

    def _wrapped_compute(self, **kwargs):
        captured["tradable_symbols"] = list(kwargs.get("tradable_symbols") or [])
        decision, prev_mode, peak_equity, fees_today, risk_day = original_compute(self, **kwargs)
        captured["reasons"] = list(decision.risk_decision.reasons)
        return decision, prev_mode, peak_equity, fees_today, risk_day

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.RiskBudgetService.compute_decision",
        _wrapped_compute,
    )

    settings = Settings(
        DRY_RUN=False,
        LIVE_TRADING=True,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        BTCTURK_API_KEY="k",
        BTCTURK_API_SECRET="s",
        SAFE_MODE=False,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "runner_mark_coverage.sqlite"),
        SYMBOLS="BTC_TRY,ETH_TRY",
    )

    assert runner.run_one_cycle(settings) == 0
    assert captured["tradable_symbols"] == ["BTCTRY"]
    assert "mark_price_missing_fail_closed" not in captured["reasons"]


def test_stage4_safety_net_recovers_single_symbol_when_all_marks_missing(monkeypatch, tmp_path) -> None:
    runner = Stage4CycleRunner()

    class SafetyNetExchange(FakeExchange):
        def get_orderbook_with_timestamp(self, symbol: str):
            self.calls.append(f"orderbook_ts:{symbol}")
            return (Decimal("100"), Decimal("102"), datetime(2025, 1, 1, tzinfo=UTC))

    exchange = SafetyNetExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    cycle_now = datetime(2025, 1, 1, tzinfo=UTC)

    def _snapshot(self, *args, **kwargs):
        del self, args, kwargs
        return MarketSnapshot(
            mark_prices={},
            orderbooks={},
            anomalies=set(),
            spreads_bps={},
            age_seconds_by_symbol={"BTCTRY": Decimal("0")},
            fetched_at_by_symbol={"BTCTRY": cycle_now},
            max_data_age_seconds=Decimal("0"),
        )

    monkeypatch.setattr(Stage4CycleRunner, "_resolve_market_snapshot", _snapshot)

    captured: dict[str, object] = {}
    original_compute = RiskBudgetService.compute_decision

    def _wrapped_compute(self, **kwargs):
        captured["tradable_symbols"] = list(kwargs.get("tradable_symbols") or [])
        captured["mark_prices"] = dict(kwargs.get("mark_prices") or {})
        return original_compute(self, **kwargs)

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.RiskBudgetService.compute_decision",
        _wrapped_compute,
    )

    settings = Settings(
        DRY_RUN=False,
        LIVE_TRADING=True,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        BTCTURK_API_KEY="k",
        BTCTURK_API_SECRET="s",
        SAFE_MODE=False,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "runner_mark_safety_net.sqlite"),
        SYMBOLS="BTC_TRY",
    )

    assert runner.run_one_cycle(settings) == 0
    assert captured["tradable_symbols"] == ["BTCTRY"]
    assert captured["mark_prices"]["BTCTRY"] == Decimal("101")


def test_stage4_safety_net_failure_logs_marker_and_continues(monkeypatch, tmp_path, caplog) -> None:
    runner = Stage4CycleRunner()

    class FailingSafetyNetExchange(FakeExchange):
        def get_orderbook_with_timestamp(self, symbol: str):
            self.calls.append(f"orderbook_ts:{symbol}")
            raise RuntimeError("boom")

    exchange = FailingSafetyNetExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    cycle_now = datetime(2025, 1, 1, tzinfo=UTC)

    def _snapshot(self, *args, **kwargs):
        del self, args, kwargs
        return MarketSnapshot(
            mark_prices={},
            orderbooks={},
            anomalies=set(),
            spreads_bps={},
            age_seconds_by_symbol={"BTCTRY": Decimal("0")},
            fetched_at_by_symbol={"BTCTRY": cycle_now},
            max_data_age_seconds=Decimal("0"),
        )

    monkeypatch.setattr(Stage4CycleRunner, "_resolve_market_snapshot", _snapshot)

    settings = Settings(
        DRY_RUN=False,
        LIVE_TRADING=True,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        BTCTURK_API_KEY="k",
        BTCTURK_API_SECRET="s",
        SAFE_MODE=False,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "runner_mark_safety_net_fail.sqlite"),
        SYMBOLS="BTC_TRY",
    )

    with caplog.at_level(logging.WARNING):
        assert runner.run_one_cycle(settings) == 0

    assert any(record.message == "stage4_mark_price_safety_net_failed" for record in caplog.records)
