from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from btcbot.adapters.exchange_stage4 import OrderAck
from btcbot.config import Settings
from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import OrderSide, PairInfo
from btcbot.domain.stage4 import (
    ExchangeRules,
    LifecycleAction,
    LifecycleActionType,
    Order,
    PnLSnapshot,
    Position,
    now_utc,
)
from btcbot.services.accounting_service_stage4 import AccountingIntegrityError, AccountingService
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.execution_service_stage4 import ExecutionService
from btcbot.services.order_lifecycle_service import OrderLifecycleService
from btcbot.services.reconcile_service import ReconcileService
from btcbot.services.risk_policy import RiskDecision, RiskPolicy
from btcbot.services.state_store import StateStore


class FakeExchangeStage4:
    def __init__(self) -> None:
        self.submits: list[tuple[str, Decimal, Decimal, str]] = []
        self.cancels: list[str] = []
        self.fills: list[TradeFill] = []
        self.last_since_ms: int | None = None

    def get_exchange_info(self) -> list[PairInfo]:
        return [
            PairInfo(
                pairSymbol="BTC_TRY",
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("100"),
                tickSize=Decimal("0.1"),
                stepSize=Decimal("0.0001"),
            )
        ]

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_order_id: str,
    ) -> OrderAck:
        assert isinstance(price, Decimal)
        assert isinstance(qty, Decimal)
        self.submits.append((symbol, price, qty, client_order_id))
        return OrderAck(exchange_order_id=f"ex-{client_order_id}", status="submitted")

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool:
        self.cancels.append(exchange_order_id)
        return True

    def cancel_order_by_client_order_id(self, client_order_id: str) -> bool:
        self.cancels.append(f"coid-{client_order_id}")
        return True

    def get_recent_fills(self, symbol: str, since_ms: int | None = None) -> list[TradeFill]:
        del symbol
        self.last_since_ms = since_ms
        return list(self.fills)


class MissingRulesService:
    def get_rules(self, symbol: str):
        raise ValueError(f"No usable exchange rules for symbol={symbol} status=missing")


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "stage4.sqlite"))


def test_execution_enforces_live_ack_and_kill_switch(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    rules = ExchangeRulesService(exchange)

    with pytest.raises(ValueError):
        Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        )

    with pytest.raises(ValueError):
        Settings(
            DRY_RUN=False,
            KILL_SWITCH=True,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        )

    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=True, LIVE_TRADING=False),
        rules_service=rules,
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("1000"),
        qty=Decimal("0.2"),
        reason="test",
        client_order_id="cid-1",
    )
    assert svc.execute([action]) == 0


def test_execution_contract_live_submits_and_dry_run_simulates(store: StateStore) -> None:
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="contract",
        client_order_id="cid-contract",
    )

    dry_exchange = FakeExchangeStage4()
    dry_service = ExecutionService(
        exchange=dry_exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False, SAFE_MODE=False),
        rules_service=ExchangeRulesService(dry_exchange),
    )
    dry_report = dry_service.execute_with_report([action])

    assert dry_report.submitted == 0
    assert dry_report.simulated == 1
    assert dry_exchange.submits == []

    live_exchange = FakeExchangeStage4()
    live_service = ExecutionService(
        exchange=live_exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        ),
        rules_service=ExchangeRulesService(live_exchange),
    )
    live_action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="contract",
        client_order_id="cid-contract-live",
    )
    live_report = live_service.execute_with_report([live_action])

    assert live_report.submitted == 1
    assert live_report.simulated == 0
    assert len(live_exchange.submits) == 1


def test_decimal_end_to_end_and_idempotent_submit(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        ),
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.456"),
        qty=Decimal("1.23456"),
        reason="test",
        client_order_id="cid-2",
    )

    assert svc.execute([action]) == 1
    assert svc.execute([action]) == 0
    assert len(exchange.submits) == 1
    _, submitted_price, submitted_qty, submitted_client_id = exchange.submits[0]
    assert submitted_price == Decimal("123.4")
    assert submitted_qty == Decimal("1.2345")
    assert len(submitted_client_id) <= 50
    assert submitted_client_id != action.client_order_id


def test_cancel_lookup_works_when_action_has_no_exchange_id(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-cancel",
        exchange_order_id="ex-77",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
    )
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        ),
        rules_service=ExchangeRulesService(exchange),
    )
    cancel = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="stale",
        client_order_id="cid-cancel",
    )
    assert svc.execute([cancel]) == 1
    assert exchange.cancels == ["ex-77"]


def test_lifecycle_replace_plan_and_ordering() -> None:
    now = datetime.now(UTC)
    open_order = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now - timedelta(seconds=300),
        updated_at=now - timedelta(seconds=300),
        exchange_order_id="ex-1",
        client_order_id="cid-old",
        mode="live",
    )
    intent = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("101"),
        qty=Decimal("1"),
        status="new",
        created_at=now,
        updated_at=now,
        client_order_id="cid-new",
    )
    plan = OrderLifecycleService(stale_after_sec=60).plan(
        [intent], [open_order], mid_price=Decimal("100")
    )
    assert [item.action_type for item in plan.actions] == [
        LifecycleActionType.CANCEL,
        LifecycleActionType.SUBMIT,
    ]


def test_risk_profit_enforcement_and_projection() -> None:
    policy = RiskPolicy(
        max_open_orders=2,
        max_position_notional_try=Decimal("1000"),
        max_daily_loss_try=Decimal("200"),
        max_drawdown_pct=Decimal("20"),
        fee_bps_taker=Decimal("10"),
        slippage_bps_buffer=Decimal("10"),
        min_profit_bps=Decimal("20"),
    )
    sell_action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="sell",
        price=Decimal("100.2"),
        qty=Decimal("1"),
        reason="take-profit",
        client_order_id="cid-sell",
    )
    pnl = PnLSnapshot(
        total_equity_try=Decimal("1000"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now_utc(),
        realized_total_try=Decimal("0"),
    )
    positions = {
        "BTC_TRY": Position(
            symbol="BTC_TRY",
            qty=Decimal("1"),
            avg_cost_try=Decimal("100"),
            realized_pnl_try=Decimal("0"),
            last_update_ts=now_utc(),
        )
    }

    accepted, decisions = policy.filter_actions(
        [sell_action],
        open_orders_count=0,
        current_position_notional_try=Decimal("100"),
        pnl=pnl,
        positions_by_symbol=positions,
    )
    assert accepted == []
    assert decisions[0].reason == "min_profit_threshold"


def test_accounting_equity_realized_today_fee_and_oversell(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = AccountingService(exchange=exchange, state_store=store)

    buy_fill = TradeFill(
        fill_id="f-buy",
        order_id="o-buy",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=Decimal("100"),
        qty=Decimal("1"),
        fee=Decimal("1"),
        fee_currency="TRY",
        ts=now_utc(),
    )
    exchange.fills = [buy_fill]
    fetch = svc.fetch_new_fills("BTC_TRY")
    snapshot = svc.apply_fills(
        fetch.fills, mark_prices={"BTC_TRY": Decimal("110")}, try_cash=Decimal("500")
    )
    assert snapshot.total_equity_try == Decimal("601")

    sell_bad_fee = TradeFill(
        fill_id="f-sell",
        order_id="o-sell",
        symbol="BTC_TRY",
        side=OrderSide.SELL,
        price=Decimal("120"),
        qty=Decimal("1"),
        fee=Decimal("0.1"),
        fee_currency="USDT",
        ts=now_utc(),
    )
    exchange.fills = [sell_bad_fee]
    fills2 = svc.fetch_new_fills("BTC_TRY")
    snapshot2 = svc.apply_fills(
        fills2.fills, mark_prices={"BTC_TRY": Decimal("120")}, try_cash=Decimal("620")
    )
    assert snapshot2.realized_today_try > Decimal("0")

    oversell = TradeFill(
        fill_id="f-over",
        order_id="o-over",
        symbol="BTC_TRY",
        side=OrderSide.SELL,
        price=Decimal("120"),
        qty=Decimal("2"),
        fee=Decimal("0"),
        fee_currency="TRY",
        ts=now_utc(),
    )
    exchange.fills = [oversell]
    fills3 = svc.fetch_new_fills("BTC_TRY")
    with pytest.raises(AccountingIntegrityError):
        svc.apply_fills(
            fills3.fills, mark_prices={"BTC_TRY": Decimal("120")}, try_cash=Decimal("620")
        )


def test_reconcile_enrichment_and_missing_client_id() -> None:
    now = now_utc()
    db_order = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now,
        updated_at=now,
        exchange_order_id=None,
        client_order_id="cid-1",
        mode="live",
    )
    exchange_order = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now,
        updated_at=now,
        exchange_order_id="ex-1",
        client_order_id="cid-1",
        mode="live",
    )
    external_missing_client = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("99"),
        qty=Decimal("1"),
        status="open",
        created_at=now,
        updated_at=now,
        exchange_order_id="ex-ext",
        client_order_id=None,
        mode="live",
    )
    result = ReconcileService().resolve(
        exchange_open_orders=[exchange_order, external_missing_client],
        db_open_orders=[db_order],
    )
    assert result.enrich_exchange_ids == [("cid-1", "ex-1")]
    assert len(result.external_missing_client_id) == 1


def test_reconcile_fail_closed_does_not_clear_unknown_for_failed_symbol(store: StateStore) -> None:
    store.record_stage4_order_error(
        client_order_id="cid-unknown-btc",
        reason="submit_uncertain_outcome",
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="unknown",
    )

    db_open_orders = store.list_stage4_open_orders(include_unknown=True)
    reconcile_result = ReconcileService().resolve(
        exchange_open_orders=[],
        db_open_orders=db_open_orders,
        failed_symbols={"BTCTRY"},
    )

    for client_order_id in reconcile_result.mark_unknown_closed:
        store.mark_stage4_unknown_closed(client_order_id)

    assert reconcile_result.mark_unknown_closed == []
    assert store.stage4_has_unknown_orders()
    assert store.get_stage4_order_by_client_id("cid-unknown-btc").status == "unknown"


def test_accounting_fetch_new_fills_initializes_since_ms_with_lookback_when_cursor_missing(
    store: StateStore,
) -> None:
    exchange = FakeExchangeStage4()
    svc = AccountingService(exchange=exchange, state_store=store, lookback_minutes=30)

    before_ms = int((datetime.now(UTC) - timedelta(minutes=30)).timestamp() * 1000)
    svc.fetch_new_fills("BTC_TRY")
    after_ms = int((datetime.now(UTC) - timedelta(minutes=30)).timestamp() * 1000)

    assert exchange.last_since_ms is not None
    assert before_ms <= exchange.last_since_ms <= after_ms


def test_accounting_fetch_new_fills_applies_lookback_even_with_cursor(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = AccountingService(exchange=exchange, state_store=store, lookback_minutes=30)
    store.set_cursor("fills_cursor:BTCTRY", "3600000")

    svc.fetch_new_fills("BTC_TRY")

    assert exchange.last_since_ms == 1800000


def test_cancel_does_not_hit_exchange_when_live_not_armed(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-cancel-nonlive",
        exchange_order_id="ex-88",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="dry_run",
    )
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=False, KILL_SWITCH=False, LIVE_TRADING=False),
        rules_service=ExchangeRulesService(exchange),
    )
    cancel = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="stale",
        client_order_id="cid-cancel-nonlive",
    )

    assert svc.execute([cancel]) == 1
    assert exchange.cancels == []


def test_submit_idempotency_persists_single_row(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        ),
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("150"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="cid-idem-one",
    )

    assert svc.execute([action]) == 1
    assert svc.execute([action]) == 0

    with store._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM stage4_orders WHERE client_order_id = ?",
            ("cid-idem-one",),
        ).fetchone()

    assert row is not None
    assert row["c"] == 1


def test_failed_submit_allows_retry(store: StateStore) -> None:
    class FailingExchange(FakeExchangeStage4):
        def submit_limit_order(self, symbol, side, price, qty, client_order_id):
            del symbol, side, price, qty, client_order_id
            raise RuntimeError("exchange down")

    exchange = FailingExchange()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        ),
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("150"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="cid-failed-retry",
    )

    with pytest.raises(RuntimeError):
        svc.execute([action])
    with pytest.raises(RuntimeError):
        svc.execute([action])


def test_open_order_dedupe_only_while_open(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        ),
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("150"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="cid-open-dedupe",
    )

    assert svc.execute([action]) == 1
    assert svc.execute([action]) == 0
    store.record_stage4_order_canceled("cid-open-dedupe")
    assert svc.execute([action]) == 1


def test_execution_rejects_and_continues_when_rules_missing(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False),
        rules_service=MissingRulesService(),
    )
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("1000"),
            qty=Decimal("1"),
            reason="test",
            client_order_id="cid-missing-rules",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("1000"),
            qty=Decimal("1"),
            reason="cancel",
            client_order_id="cid-cancel-after-rules",
            exchange_order_id="ex-cancel-1",
        ),
    ]

    report = svc.execute_with_report(actions)

    assert report.rejected == 1
    assert report.rejected_min_notional == 0
    assert report.canceled == 1
    rejected_order = store.get_stage4_order_by_client_id("cid-missing-rules")
    assert rejected_order is not None
    with store._connect() as conn:
        row = conn.execute(
            "SELECT last_error FROM stage4_orders WHERE client_order_id=?",
            ("cid-missing-rules",),
        ).fetchone()
    assert row is not None
    assert row["last_error"] == "missing_exchange_rules"


def test_execution_report_tracks_only_min_notional_rejections(tmp_path) -> None:
    store = StateStore(str(tmp_path / "stage4_reject_breakdown.sqlite"))

    class MixedRulesService:
        def resolve_boundary(self, symbol: str):
            del symbol
            return type("R", (), {"rules": None, "resolution": type("D", (), {"status": "missing"})()})()

    class MinNotionalRulesService:
        def resolve_boundary(self, symbol: str):
            del symbol
            return type(
                "R",
                (),
                {
                    "rules": ExchangeRules(
                        tick_size=Decimal("0.1"),
                        step_size=Decimal("0.0001"),
                        min_notional_try=Decimal("200"),
                        price_precision=2,
                        qty_precision=4,
                    )
                },
            )()

    exchange = FakeExchangeStage4()
    svc_missing = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False),
        rules_service=MixedRulesService(),
    )
    report_missing = svc_missing.execute_with_report(
        [
            LifecycleAction(
                action_type=LifecycleActionType.SUBMIT,
                symbol="BTC_TRY",
                side="buy",
                price=Decimal("100"),
                qty=Decimal("0.1"),
                reason="missing_rules",
                client_order_id="cid-missing-only",
            )
        ]
    )

    svc_min = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False),
        rules_service=MinNotionalRulesService(),
    )
    report_min = svc_min.execute_with_report(
        [
            LifecycleAction(
                action_type=LifecycleActionType.SUBMIT,
                symbol="BTC_TRY",
                side="buy",
                price=Decimal("100"),
                qty=Decimal("1"),
                reason="min_notional",
                client_order_id="cid-min-only",
            )
        ]
    )

    assert report_missing.rejected == 1
    assert report_missing.rejected_min_notional == 0
    assert report_min.rejected == 1
    assert report_min.rejected_min_notional == 1


def test_replace_worst_case_exposure_blocked() -> None:
    policy = RiskPolicy(
        max_open_orders=10,
        max_position_notional_try=Decimal("10000"),
        max_daily_loss_try=Decimal("200"),
        max_drawdown_pct=Decimal("20"),
        fee_bps_taker=Decimal("10"),
        slippage_bps_buffer=Decimal("10"),
        min_profit_bps=Decimal("20"),
        replace_inflight_budget_per_symbol_try=Decimal("150"),
        max_gross_exposure_try=Decimal("500"),
    )
    old_order = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now_utc(),
        updated_at=now_utc(),
        client_order_id="old",
    )
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new",
            replace_for_client_order_id="old",
        ),
    ]
    pnl = PnLSnapshot(
        total_equity_try=Decimal("1000"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now_utc(),
        realized_total_try=Decimal("0"),
    )

    accepted, decisions = policy.filter_actions(
        actions,
        open_orders_count=1,
        current_position_notional_try=Decimal("0"),
        pnl=pnl,
        positions_by_symbol={},
        open_orders_by_client_id={"old": old_order},
    )

    assert [a.action_type for a in accepted] == [LifecycleActionType.CANCEL]
    assert any(d.reason == "replace_worst_case_exposure_blocked" and not d.accepted for d in decisions)


def test_risk_replace_worst_case_within_budget_accepts_submit() -> None:
    policy = RiskPolicy(
        max_open_orders=10,
        max_position_notional_try=Decimal("10000"),
        max_daily_loss_try=Decimal("200"),
        max_drawdown_pct=Decimal("20"),
        fee_bps_taker=Decimal("10"),
        slippage_bps_buffer=Decimal("10"),
        min_profit_bps=Decimal("20"),
        replace_inflight_budget_per_symbol_try=Decimal("250"),
        max_gross_exposure_try=Decimal("500"),
    )
    old_order = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now_utc(),
        updated_at=now_utc(),
        client_order_id="old",
    )
    submit = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="replace_submit",
        client_order_id="new",
        replace_for_client_order_id="old",
    )
    pnl = PnLSnapshot(
        total_equity_try=Decimal("1000"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now_utc(),
        realized_total_try=Decimal("0"),
    )

    accepted, decisions = policy.filter_actions(
        [submit],
        open_orders_count=1,
        current_position_notional_try=Decimal("0"),
        pnl=pnl,
        positions_by_symbol={},
        open_orders_by_client_id={"old": old_order},
    )

    assert accepted == [submit]
    assert any(d.reason == "accepted" and d.accepted for d in decisions)


def test_cancel_does_not_mutate_replace_budget() -> None:
    policy = RiskPolicy(
        max_open_orders=10,
        max_position_notional_try=Decimal("10000"),
        max_daily_loss_try=Decimal("200"),
        max_drawdown_pct=Decimal("20"),
        fee_bps_taker=Decimal("10"),
        slippage_bps_buffer=Decimal("10"),
        min_profit_bps=Decimal("20"),
        replace_inflight_budget_per_symbol_try=Decimal("150"),
        max_gross_exposure_try=Decimal("500"),
    )
    old_order = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now_utc(),
        updated_at=now_utc(),
        client_order_id="old",
    )
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("9999"),
            qty=Decimal("123"),
            reason="replace_cancel",
            client_order_id="old",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new",
            replace_for_client_order_id="old",
        ),
    ]
    pnl = PnLSnapshot(
        total_equity_try=Decimal("1000"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now_utc(),
        realized_total_try=Decimal("0"),
    )

    accepted_first, decisions_first = policy.filter_actions(
        actions,
        open_orders_count=1,
        current_position_notional_try=Decimal("0"),
        pnl=pnl,
        positions_by_symbol={},
        open_orders_by_client_id={"old": old_order},
    )
    accepted_second, decisions_second = policy.filter_actions(
        actions,
        open_orders_count=1,
        current_position_notional_try=Decimal("0"),
        pnl=pnl,
        positions_by_symbol={},
        open_orders_by_client_id={"old": old_order},
    )

    assert [a.action_type for a in accepted_first] == [LifecycleActionType.CANCEL]
    assert [a.action_type for a in accepted_second] == [LifecycleActionType.CANCEL]
    assert decisions_first == decisions_second


def test_replace_submit_without_old_order_lookup_uses_new_notional_and_global_exposure() -> None:
    policy = RiskPolicy(
        max_open_orders=10,
        max_position_notional_try=Decimal("10000"),
        max_daily_loss_try=Decimal("200"),
        max_drawdown_pct=Decimal("20"),
        fee_bps_taker=Decimal("10"),
        slippage_bps_buffer=Decimal("10"),
        min_profit_bps=Decimal("20"),
        replace_inflight_budget_per_symbol_try=Decimal("1000"),
        max_gross_exposure_try=Decimal("500"),
    )
    submit = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="replace_submit",
        client_order_id="new",
        replace_for_client_order_id="missing-old-order",
    )
    pnl = PnLSnapshot(
        total_equity_try=Decimal("1000"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now_utc(),
        realized_total_try=Decimal("0"),
    )

    accepted, decisions = policy.filter_actions(
        [submit],
        open_orders_count=0,
        current_position_notional_try=Decimal("450"),
        pnl=pnl,
        positions_by_symbol={},
        open_orders_by_client_id={},
    )

    assert accepted == []
    assert decisions == [
        RiskDecision(
            action=submit,
            accepted=False,
            reason="replace_worst_case_exposure_blocked",
        )
    ]


def test_stage4_uncertain_submit_records_unknown_and_freezes_submits(store: StateStore) -> None:
    class UncertainExchange(FakeExchangeStage4):
        def submit_limit_order(self, symbol: str, side: str, price: Decimal, qty: Decimal, client_order_id: str):
            raise TimeoutError("submit-uncertain")

    exchange = UncertainExchange()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=False,
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        ),
        rules_service=ExchangeRulesService(exchange),
    )

    submit_1 = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="uncertain",
        client_order_id="cid-uncertain-1",
    )
    submit_2 = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="blocked",
        client_order_id="cid-uncertain-2",
    )

    report1 = svc.execute_with_report([submit_1])
    report2 = svc.execute_with_report([submit_2])

    assert report1.executed_total == 0
    assert report2.executed_total == 0
    assert store.stage4_has_unknown_orders()
    assert store.get_stage4_order_by_client_id("cid-uncertain-1").status == "unknown"

    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-open-cancel-ok",
        exchange_order_id="ex-open-cancel-ok",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
    )
    cancel = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="cancel",
        client_order_id="cid-open-cancel-ok",
    )
    cancel_report = svc.execute_with_report([cancel])
    assert cancel_report.canceled == 1
