from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from btcbot.adapters.exchange_stage4 import OrderAck
from btcbot.config import Settings
from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import OrderSide, PairInfo
from btcbot.domain.stage4 import (
    ExchangeRules,
    Fill,
    LifecycleAction,
    LifecycleActionType,
    Order,
    PnLSnapshot,
    Position,
    Quantizer,
    now_utc,
)
from btcbot.services.accounting_service_stage4 import AccountingIntegrityError, AccountingService
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.execution_service_stage4 import (
    REASON_TOKEN_EXCHANGE_1123,
    REASON_TOKEN_GATE_1123,
    ExecutionService,
)
from btcbot.services.ledger_service import LedgerService
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
        self.open_orders_by_symbol: dict[str, list[Order]] = {}

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

    def list_open_orders(self, symbol: str) -> list[Order]:
        return list(self.open_orders_by_symbol.get(symbol, []))

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


def test_quantize_qty_up_ceil_behavior() -> None:
    rules = ExchangeRules(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.0001"),
        min_notional_try=Decimal("100"),
        price_precision=2,
        qty_precision=4,
    )

    assert Quantizer.quantize_qty_up(Decimal("0"), rules) == Decimal("0")
    assert Quantizer.quantize_qty_up(Decimal("1.2345"), rules) == Decimal("1.2345")
    assert Quantizer.quantize_qty_up(Decimal("1.23451"), rules) == Decimal("1.2346")


def test_quantize_qty_up_precision_only_rounds_up() -> None:
    rules = ExchangeRules(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0"),
        min_notional_try=Decimal("100"),
        price_precision=2,
        qty_precision=4,
    )

    assert Quantizer.quantize_qty_up(Decimal("1.2345"), rules) == Decimal("1.2345")
    assert Quantizer.quantize_qty_up(Decimal("1.23451"), rules) == Decimal("1.2346")


def test_stage4_submit_applies_min_notional_rounding_fix(store: StateStore) -> None:
    class MinNotionalRulesService:
        def resolve_boundary(self, symbol: str):
            del symbol
            return type(
                "R",
                (),
                {
                    "rules": ExchangeRules(
                        tick_size=Decimal("0.01"),
                        step_size=Decimal("0.0001"),
                        min_notional_try=Decimal("100"),
                        price_precision=2,
                        qty_precision=4,
                    )
                },
            )()

    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False),
        rules_service=MinNotionalRulesService(),
    )

    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100.005"),
        qty=Decimal("0.99996"),
        reason="rounding_fix",
        client_order_id="cid-rounding-fix",
    )

    report = svc.execute_with_report([action])

    assert report.rejected == 0
    assert report.rejected_min_notional == 0
    assert report.simulated == 1
    order = store.get_stage4_order_by_client_id("cid-rounding-fix")
    assert order is not None
    assert order.price == Decimal("100.00")
    assert order.qty == Decimal("1.0000")
    assert order.price * order.qty >= Decimal("100")


def test_stage4_submit_keeps_below_min_intent_rejected(store: StateStore) -> None:
    class MinNotionalRulesService:
        def resolve_boundary(self, symbol: str):
            del symbol
            return type(
                "R",
                (),
                {
                    "rules": ExchangeRules(
                        tick_size=Decimal("0.01"),
                        step_size=Decimal("0.0001"),
                        min_notional_try=Decimal("100"),
                        price_precision=2,
                        qty_precision=4,
                    )
                },
            )()

    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False),
        rules_service=MinNotionalRulesService(),
    )

    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100.005"),
        qty=Decimal("0.99990"),
        reason="below_min_intent",
        client_order_id="cid-below-min-intent",
    )

    report = svc.execute_with_report([action])

    assert report.rejected == 1
    assert report.rejected_min_notional == 1
    assert report.simulated == 0
    assert report.rejects_breakdown["min_total"] == 1
    assert len(report.reject_details) == 1
    detail = report.reject_details[0]
    assert detail["reason"] == "min_total"
    assert detail["min_required_settings"] == "10.0"
    assert detail["min_required_exchange_rule"] == "100"
    assert detail["q_price"] == "100.00"
    assert detail["q_qty"] == "0.9999"
    assert detail["total_try"] == "99.990000"
    rejected = store.get_stage4_order_by_client_id("cid-below-min-intent")
    assert rejected is not None
    assert rejected.status == "rejected"



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






def test_execution_honors_effective_killswitch_attribute_for_submit(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    settings = Settings(
        DRY_RUN=False,
        KILL_SWITCH=False,
        kill_switch_effective=True,
        LIVE_TRADING=False,
        SAFE_MODE=False,
    )
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=settings,
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="db_kill",
        client_order_id="cid-db-kill",
    )

    report = svc.execute_with_report([action])

    assert report.submitted == 0
    assert exchange.submits == []

def test_execution_killswitch_allows_cancel_by_default(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=False, KILL_SWITCH=True, LIVE_TRADING=False, SAFE_MODE=False),
        rules_service=ExchangeRulesService(exchange),
    )
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-cancel",
        exchange_client_id="ex-cid-cancel",
        exchange_order_id="ex-order-1",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="open",
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test_cancel",
        client_order_id="cid-cancel",
        exchange_order_id="ex-order-1",
    )

    report = svc.execute_with_report([action])

    assert report.canceled == 1
    assert exchange.cancels == []
    order = store.get_stage4_order_by_client_id("cid-cancel")
    assert order is not None and order.status == "canceled"


def test_execution_killswitch_freeze_all_blocks_cancel(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(
            DRY_RUN=False,
            KILL_SWITCH=True,
            KILL_SWITCH_FREEZE_ALL=True,
            LIVE_TRADING=False,
            SAFE_MODE=False,
        ),
        rules_service=ExchangeRulesService(exchange),
    )
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-cancel-freeze",
        exchange_client_id="ex-cid-cancel-freeze",
        exchange_order_id="ex-order-2",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="open",
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test_cancel",
        client_order_id="cid-cancel-freeze",
        exchange_order_id="ex-order-2",
    )

    report = svc.execute_with_report([action])

    assert report.canceled == 0
    assert exchange.cancels == []



def test_execution_unknown_freeze_blocks_submit_persisted(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    settings = Settings(DRY_RUN=False, KILL_SWITCH=False, LIVE_TRADING=False, SAFE_MODE=False)
    settings.process_role = "trader"
    store.stage4_set_freeze("trader", reason="unknown_open_orders", details={"count": 1})
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=settings,
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="cid-freeze-submit",
    )

    report = svc.execute_with_report([action])

    assert report.submitted == 0
    assert report.simulated == 0
    assert exchange.submits == []


def test_execution_unknown_freeze_allows_cancel_when_not_freeze_all(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    settings = Settings(DRY_RUN=False, KILL_SWITCH=False, LIVE_TRADING=False, SAFE_MODE=False)
    settings.process_role = "trader"
    store.stage4_set_freeze("trader", reason="unknown_open_orders", details={"count": 2})
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=settings,
        rules_service=ExchangeRulesService(exchange),
    )
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-freeze-cancel",
        exchange_client_id="ex-cid-freeze-cancel",
        exchange_order_id="ex-order-freeze-cancel",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="open",
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test_cancel",
        client_order_id="cid-freeze-cancel",
        exchange_order_id="ex-order-freeze-cancel",
    )

    report = svc.execute_with_report([action])

    assert report.canceled == 1


def test_execution_unknown_freeze_blocks_cancel_when_freeze_all(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    settings = Settings(
        DRY_RUN=False,
        KILL_SWITCH=False,
        KILL_SWITCH_FREEZE_ALL=True,
        LIVE_TRADING=False,
        SAFE_MODE=False,
    )
    settings.process_role = "trader"
    store.stage4_set_freeze("trader", reason="unknown_open_orders", details={"count": 2})
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=settings,
        rules_service=ExchangeRulesService(exchange),
    )
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-freeze-all-cancel",
        exchange_client_id="ex-cid-freeze-all-cancel",
        exchange_order_id="ex-order-freeze-all-cancel",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="open",
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test_cancel",
        client_order_id="cid-freeze-all-cancel",
        exchange_order_id="ex-order-freeze-all-cancel",
    )

    report = svc.execute_with_report([action])

    assert report.canceled == 0

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


def test_execution_dry_run_never_calls_submit_or_cancel_and_emits_suppression_counter(
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CaptureInstrumentation:
        def __init__(self) -> None:
            self.counters: list[tuple[str, int]] = []

        def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
            del attrs
            self.counters.append((name, value))

    capture = _CaptureInstrumentation()
    monkeypatch.setattr(
        "btcbot.services.execution_service_stage4.get_instrumentation",
        lambda: capture,
    )

    exchange = FakeExchangeStage4()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False, SAFE_MODE=False),
        rules_service=ExchangeRulesService(exchange),
    )

    submit_action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="contract",
        client_order_id="cid-dryrun-submit",
    )
    cancel_action = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="contract",
        client_order_id="cid-dryrun-submit",
        exchange_order_id="ex-cid-dryrun-submit",
    )

    report = service.execute_with_report([submit_action, cancel_action])

    assert report.submitted == 0
    assert report.simulated >= 1
    assert exchange.submits == []
    assert exchange.cancels == []
    assert any(
        name == "dryrun_submission_suppressed_total" and value >= 1
        for name, value in capture.counters
    )


def test_execution_dry_run_never_calls_execution_wrapper_write_methods(
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = FakeExchangeStage4()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, LIVE_TRADING=False, SAFE_MODE=False),
        rules_service=ExchangeRulesService(exchange),
    )

    monkeypatch.setattr(
        service.execution_wrapper,
        "submit_limit_order",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit wrapper must not be called")),
    )
    monkeypatch.setattr(
        service.execution_wrapper,
        "cancel_order",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("cancel wrapper must not be called")),
    )

    submit_action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="contract",
        client_order_id="cid-dryrun-no-wrapper-submit",
    )
    cancel_action = LifecycleAction(
        action_type=LifecycleActionType.CANCEL,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1.2"),
        reason="contract",
        client_order_id="cid-dryrun-no-wrapper-submit",
        exchange_order_id="ex-cid-dryrun-no-wrapper-submit",
    )

    report = service.execute_with_report([submit_action, cancel_action])
    assert report.submitted == 0
    assert report.simulated >= 1


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


def test_stage4_fill_import_idempotency_with_lookback(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    ts = now_utc()
    exchange.fills = [
        TradeFill(
            fill_id="fill-A",
            order_id="ord-A",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            qty=Decimal("1"),
            fee=Decimal("1"),
            fee_currency="TRY",
            ts=ts,
        ),
        TradeFill(
            fill_id="fill-B",
            order_id="ord-B",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=Decimal("110"),
            qty=Decimal("1"),
            fee=Decimal("1"),
            fee_currency="TRY",
            ts=ts + timedelta(milliseconds=1),
        ),
    ]
    accounting = AccountingService(exchange=exchange, state_store=store, lookback_minutes=30)
    ledger = LedgerService(state_store=store, logger=logging.getLogger(__name__))

    first = accounting.fetch_new_fills("BTC_TRY")
    assert first.fills_seen == 2
    with store.transaction():
        ledger.ingest_exchange_updates(first.fills)
        accounting.apply_fills(
            first.fills,
            mark_prices={"BTCTRY": Decimal("110")},
            try_cash=Decimal("1000"),
        )
        assert first.cursor_after is not None
        store.set_cursor("fills_cursor:BTCTRY", first.cursor_after)

    second = accounting.fetch_new_fills("BTC_TRY")
    # lookback intentionally re-sees already imported fills
    assert second.fills_seen == 2
    with store.transaction():
        second_ingest = ledger.ingest_exchange_updates(second.fills)
        accounting.apply_fills(
            second.fills,
            mark_prices={"BTCTRY": Decimal("110")},
            try_cash=Decimal("1000"),
        )
        assert second.cursor_after is not None
        store.set_cursor("fills_cursor:BTCTRY", second.cursor_after)

    with store._connect() as conn:
        fill_events = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_events WHERE type='FILL'"
        ).fetchone()["c"]
        fee_events = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_events WHERE type='FEE'"
        ).fetchone()["c"]
        applied_rows = conn.execute("SELECT COUNT(*) AS c FROM applied_fills").fetchone()["c"]
    assert fill_events == 2
    assert fee_events == 2
    assert applied_rows == 2
    assert second_ingest.events_ignored >= 2


def test_reconcile_service_resolve_covers_unknown_external_and_enrichment() -> None:
    now = now_utc()
    db_missing_on_exchange = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="unknown",
        created_at=now,
        updated_at=now,
        exchange_order_id="ex-missing",
        client_order_id="cid-missing",
        mode="live",
    )
    db_missing_exchange_id = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("101"),
        qty=Decimal("1"),
        status="open",
        created_at=now,
        updated_at=now,
        exchange_order_id=None,
        client_order_id="cid-enrich",
        mode="live",
    )
    exchange_known = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("101"),
        qty=Decimal("1"),
        status="open",
        created_at=now,
        updated_at=now,
        exchange_order_id="ex-enrich",
        client_order_id="cid-enrich",
        mode="live",
    )
    exchange_external = Order(
        symbol="BTC_TRY",
        side="sell",
        type="limit",
        price=Decimal("120"),
        qty=Decimal("0.5"),
        status="open",
        created_at=now,
        updated_at=now,
        exchange_order_id="ex-external",
        client_order_id="cid-external",
        mode="live",
    )

    result = ReconcileService().resolve(
        exchange_open_orders=[exchange_known, exchange_external],
        db_open_orders=[db_missing_on_exchange, db_missing_exchange_id],
    )

    assert result.mark_unknown_closed == ["cid-missing"]
    assert result.enrich_exchange_ids == [("cid-enrich", "ex-enrich")]
    assert len(result.import_external) == 1
    assert result.import_external[0].client_order_id == "cid-external"
    assert result.import_external[0].mode == "external"


def test_ledger_report_fee_conversion_missing_is_fail_closed_signal(store: StateStore) -> None:
    ts = now_utc()
    fill = Fill(
        fill_id="fill-usdt",
        order_id="order-usdt",
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        fee=Decimal("2"),
        fee_asset="USDT",
        ts=ts,
    )
    ledger = LedgerService(state_store=store, logger=logging.getLogger(__name__))
    with store.transaction():
        ledger.ingest_exchange_updates([fill])

    report = ledger.report(mark_prices={"BTCTRY": Decimal("100")}, cash_try=Decimal("1000"))
    assert report.fees_total_try == Decimal("0")
    assert report.fee_conversion_missing_currencies == ("USDT",)


def test_accounting_snapshot_and_fee_total_try_after_buy_sell(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    accounting = AccountingService(exchange=exchange, state_store=store)
    ledger = LedgerService(state_store=store, logger=logging.getLogger(__name__))
    ts = now_utc()

    fills = [
        Fill(
            fill_id="buy-1",
            order_id="ord-buy",
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            fee=Decimal("1"),
            fee_asset="TRY",
            ts=ts,
        ),
        Fill(
            fill_id="sell-1",
            order_id="ord-sell",
            symbol="BTC_TRY",
            side="sell",
            price=Decimal("120"),
            qty=Decimal("1"),
            fee=Decimal("1"),
            fee_asset="TRY",
            ts=ts + timedelta(seconds=1),
        ),
    ]

    with store.transaction():
        ledger.ingest_exchange_updates(fills)
        snapshot = accounting.apply_fills(
            fills,
            mark_prices={"BTCTRY": Decimal("120")},
            try_cash=Decimal("1000"),
        )

    # buy avg_cost=101 due to fee, sell realized=(120-101)-1=18
    assert snapshot.realized_total_try == Decimal("18")
    assert snapshot.realized_today_try == Decimal("18")

    report = ledger.report(mark_prices={"BTCTRY": Decimal("120")}, cash_try=Decimal("1000"))
    assert report.fees_total_try == Decimal("2")


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
            return type(
                "R", (), {"rules": None, "resolution": type("D", (), {"status": "missing"})()}
            )()

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
    assert any(
        d.reason == "replace_worst_case_exposure_blocked" and not d.accepted for d in decisions
    )


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
        def submit_limit_order(
            self, symbol: str, side: str, price: Decimal, qty: Decimal, client_order_id: str
        ):
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


def test_replace_group_detection() -> None:
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old-1",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-1",
            replace_for_client_order_id="old-1",
        ),
    ]
    regular, groups = ExecutionService._extract_replace_groups(actions)
    assert regular == []
    assert len(groups) == 1
    assert groups[0].submit_action.client_order_id == "new-1"
    assert groups[0].submit_count == 1
    assert groups[0].had_multiple_submits is False


def test_replace_submit_deferred_until_exchange_confirms_cancel(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    now = now_utc()
    old = Order(
        symbol="BTC_TRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="open",
        created_at=now,
        updated_at=now,
        exchange_order_id="ex-old",
        client_order_id="old",
        mode="live",
    )
    exchange.open_orders_by_symbol["BTC_TRY"] = [old]
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="old",
        exchange_order_id="ex-old",
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
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old",
            exchange_order_id="ex-old",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new",
            replace_for_client_order_id="old",
        ),
    ]

    first = svc.execute_with_report(actions)
    assert first.submitted == 0

    exchange.open_orders_by_symbol["BTC_TRY"] = []
    store.record_stage4_order_canceled("old")
    second = svc.execute_with_report(actions)
    assert second.submitted == 1


def test_replace_submit_blocked_by_unknown_freeze(store: StateStore) -> None:
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
    store.record_stage4_order_error(
        client_order_id="unknown-1",
        reason="manual_unknown",
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="unknown",
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
            exchange_order_id="ex-old",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new",
            replace_for_client_order_id="old",
        ),
    ]
    report = svc.execute_with_report(actions)
    assert report.submitted == 0
    assert exchange.submits == []


def test_replace_tx_state_does_not_regress_from_terminal(store: StateStore) -> None:
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
    exchange.open_orders_by_symbol["BTC_TRY"] = []
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="old-stable",
        exchange_order_id="ex-old-stable",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="canceled",
    )
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old-stable",
            exchange_order_id="ex-old-stable",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-stable",
            replace_for_client_order_id="old-stable",
        ),
    ]

    first = svc.execute_with_report(actions)
    assert first.submitted == 1
    regular, groups = ExecutionService._extract_replace_groups(actions)
    del regular
    tx_id = svc._replace_tx_id(groups[0])
    after_first = store.get_replace_tx(tx_id)
    assert after_first is not None
    assert after_first.state == "SUBMIT_CONFIRMED"

    second = svc.execute_with_report(actions)
    after_second = store.get_replace_tx(tx_id)
    assert second.submitted == 0
    assert after_second is not None
    assert after_second.state == "SUBMIT_CONFIRMED"


def test_blocked_reconcile_replace_tx_is_listed_open(store: StateStore) -> None:
    store.upsert_replace_tx(
        replace_tx_id="rpl:block-1",
        symbol="BTC_TRY",
        side="buy",
        old_client_order_ids=["old-1"],
        new_client_order_id="new-1",
        state="INIT",
    )
    store.update_replace_tx_state(
        replace_tx_id="rpl:block-1",
        state="BLOCKED_RECONCILE",
        last_error="still_open:old-1",
    )
    open_txs = store.list_open_replace_txs()
    assert any(
        item.replace_tx_id == "rpl:block-1" and item.state == "BLOCKED_RECONCILE"
        for item in open_txs
    )


def test_replace_multiple_submit_actions_coalesced_to_last(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    exchange.open_orders_by_symbol["BTC_TRY"] = []
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="old-multi",
        exchange_order_id="ex-old-multi",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="canceled",
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
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old-multi",
            exchange_order_id="ex-old-multi",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-multi-1",
            replace_for_client_order_id="old-multi",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("102"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-multi-2",
            replace_for_client_order_id="old-multi",
        ),
    ]

    regular_actions, groups = ExecutionService._extract_replace_groups(actions)
    assert regular_actions == []
    assert len(groups) == 1
    assert groups[0].submit_count == 2
    assert groups[0].had_multiple_submits is True
    assert groups[0].selected_submit_client_order_id == "new-multi-2"

    report = svc.execute_with_report(actions)
    assert report.submitted == 1
    assert len(exchange.submits) == 1
    assert exchange.submits[0][0] == "BTC_TRY"
    assert store.get_stage4_order_by_client_id("new-multi-1") is None
    assert store.get_stage4_order_by_client_id("new-multi-2") is not None


def test_replace_local_non_terminal_defers_even_when_exchange_cleared(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    exchange.open_orders_by_symbol["BTC_TRY"] = []
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="old-local-open",
        exchange_order_id="ex-old-local-open",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="open",
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
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old-local-open",
            exchange_order_id="ex-old-local-open",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-local-open",
            replace_for_client_order_id="old-local-open",
        ),
    ]
    report = svc.execute_with_report(actions)
    assert report.submitted == 0


def test_replace_multiple_submit_coalesce_increments_metric(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SpyMetrics:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
            del value, attrs
            self.calls.append(name)

        def gauge(self, name: str, value: float, *, attrs=None) -> None:
            del name, value, attrs

    exchange = FakeExchangeStage4()
    exchange.open_orders_by_symbol["BTC_TRY"] = []
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="old-metric",
        exchange_order_id="ex-old-metric",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
        status="canceled",
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
    svc.instrumentation = SpyMetrics()  # type: ignore[assignment]
    decision_events: list[dict] = []

    def _capture_decision(_logger, event: dict) -> None:
        del _logger
        decision_events.append(event)

    monkeypatch.setattr("btcbot.services.execution_service_stage4.emit_decision", _capture_decision)
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old-metric",
            exchange_order_id="ex-old-metric",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-metric-1",
            replace_for_client_order_id="old-metric",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("102"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-metric-2",
            replace_for_client_order_id="old-metric",
        ),
    ]
    svc.execute_with_report(actions)
    assert "replace_multiple_submits_coalesced_total" in svc.instrumentation.calls
    coalesced_events = [
        event
        for event in decision_events
        if event.get("event_name") == "replace_multiple_submits_coalesced"
    ]
    assert len(coalesced_events) == 1
    payload = coalesced_events[0]["payload"]
    assert payload["submit_count"] == 2
    assert payload["selected_submit_client_order_id"] == "new-metric-2"


def test_replace_local_missing_record_defers(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    exchange.open_orders_by_symbol["BTC_TRY"] = []
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
    actions = [
        LifecycleAction(
            action_type=LifecycleActionType.CANCEL,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            reason="replace_cancel",
            client_order_id="old-missing",
            exchange_order_id="ex-old-missing",
        ),
        LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            reason="replace_submit",
            client_order_id="new-missing",
            replace_for_client_order_id="old-missing",
        ),
    ]
    report = svc.execute_with_report(actions)
    assert report.submitted == 0


def test_execution_gate_blocks_submit_when_symbol_on_1123_cooldown(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    store.record_symbol_reject("BTC_TRY", 1123, 1000, threshold=1, cooldown_minutes=60)

    class FrozenDateTime:
        @staticmethod
        def now(tz):
            return datetime.fromtimestamp(1200, tz=tz)

    svc_module = __import__("btcbot.services.execution_service_stage4", fromlist=["datetime"])
    original_datetime = svc_module.datetime
    svc_module.datetime = FrozenDateTime
    try:
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
            price=Decimal("120"),
            qty=Decimal("1"),
            reason="gate",
            client_order_id="cid-gate",
        )
        report = svc.execute_with_report([action])
    finally:
        svc_module.datetime = original_datetime

    assert report.submitted == 0
    assert report.rejected == 1
    assert exchange.submits == []
    rejected = store.get_stage4_order_by_client_id("cid-gate")
    assert rejected is not None
    assert rejected.status == "rejected"
    with store._connect() as conn:
        last_error_row = conn.execute(
            "SELECT last_error, last_error_code FROM stage4_orders WHERE client_order_id = ?",
            ("cid-gate",),
        ).fetchone()
    assert last_error_row is not None
    assert str(last_error_row["last_error"] or "") == REASON_TOKEN_GATE_1123
    assert int(last_error_row["last_error_code"]) == 1123


def test_stage4_reject_reason_label_for_1123_gate(store: StateStore) -> None:
    exchange = FakeExchangeStage4()
    store.record_symbol_reject("BTC_TRY", 1123, 1000, threshold=1, cooldown_minutes=60)

    class FrozenDateTime:
        @staticmethod
        def now(tz):
            return datetime.fromtimestamp(1200, tz=tz)

    svc_module = __import__("btcbot.services.execution_service_stage4", fromlist=["datetime"])
    original_datetime = svc_module.datetime
    svc_module.datetime = FrozenDateTime
    try:
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
            price=Decimal("120"),
            qty=Decimal("1"),
            reason="gate",
            client_order_id="cid-gate-reason",
        )
        report = svc.execute_with_report([action])
    finally:
        svc_module.datetime = original_datetime

    assert report.rejected == 1
    assert report.rejects_breakdown["breaker_open"] == 1
    assert report.reject_details[0]["reason"] == "breaker_open"
    assert report.reject_details[0]["rejected_by_code"] == "1123"


def test_exchange_reject_1123_records_symbol_cooldown(store: StateStore) -> None:
    from btcbot.domain.models import ExchangeError

    class RejectingExchange(FakeExchangeStage4):
        def submit_limit_order(self, symbol, side, price, qty, client_order_id):  # type: ignore[override]
            raise ExchangeError(
                "rejected",
                status_code=400,
                error_code=1123,
                error_message="FAILED_MIN_TOTAL_AMOUNT",
            )

    exchange = RejectingExchange()

    class FrozenDateTime:
        @staticmethod
        def now(tz):
            return datetime.fromtimestamp(3000, tz=tz)

    svc_module = __import__("btcbot.services.execution_service_stage4", fromlist=["datetime"])
    original_datetime = svc_module.datetime
    svc_module.datetime = FrozenDateTime
    try:
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
                REJECT1123_THRESHOLD=1,
                REJECT1123_COOLDOWN_MINUTES=5,
            ),
            rules_service=ExchangeRulesService(exchange),
        )
        action = LifecycleAction(
            action_type=LifecycleActionType.SUBMIT,
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("120"),
            qty=Decimal("1"),
            reason="reject",
            client_order_id="cid-1123",
        )
        report = svc.execute_with_report([action])
    finally:
        svc_module.datetime = original_datetime

    assert report.rejected == 1
    state = store.get_symbol_cooldown("BTC_TRY", now_ts=3001)
    assert state is not None
    assert state.cooldown_until_ts == 3000 + 300
    with store._connect() as conn:
        row = conn.execute(
            "SELECT last_error, last_error_code FROM stage4_orders WHERE client_order_id = ?",
            ("cid-1123",),
        ).fetchone()
    assert row is not None
    assert str(row["last_error"] or "") == REASON_TOKEN_EXCHANGE_1123
    assert int(row["last_error_code"]) == 1123


def test_risk_policy_caps_max_open_orders() -> None:
    policy = RiskPolicy(
        max_open_orders=1,
        max_order_notional_try=Decimal("10000"),
        max_position_notional_try=Decimal("10000"),
        max_daily_loss_try=Decimal("200"),
        max_drawdown_pct=Decimal("20"),
        fee_bps_taker=Decimal("10"),
        slippage_bps_buffer=Decimal("10"),
        min_profit_bps=Decimal("20"),
    )
    submit = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="entry",
        client_order_id="cid-cap-open-orders",
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
    )

    assert accepted == []
    assert decisions == [
        RiskDecision(action=submit, accepted=False, reason="max_open_orders")
    ]


def test_risk_policy_caps_max_position_notional_buy_rejected_sell_allowed() -> None:
    policy = RiskPolicy(
        max_open_orders=10,
        max_order_notional_try=Decimal("10000"),
        max_position_notional_try=Decimal("100"),
        max_daily_loss_try=Decimal("200"),
        max_drawdown_pct=Decimal("20"),
        fee_bps_taker=Decimal("10"),
        slippage_bps_buffer=Decimal("10"),
        min_profit_bps=Decimal("20"),
    )
    buy_submit = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("50"),
        qty=Decimal("1"),
        reason="entry",
        client_order_id="cid-cap-pos-buy",
    )
    sell_submit = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="sell",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="de-risk",
        client_order_id="cid-cap-pos-sell",
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
            avg_cost_try=Decimal("90"),
            realized_pnl_try=Decimal("0"),
            last_update_ts=now_utc(),
        )
    }

    accepted, decisions = policy.filter_actions(
        [buy_submit, sell_submit],
        open_orders_count=0,
        current_position_notional_try=Decimal("60"),
        pnl=pnl,
        positions_by_symbol=positions,
    )

    assert accepted == [sell_submit]
    assert decisions == [
        RiskDecision(action=buy_submit, accepted=False, reason="max_position_notional_try"),
        RiskDecision(action=sell_submit, accepted=True, reason="accepted"),
    ]


def test_execution_service_final_guard_max_order_notional_blocks_submit(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _CaptureInstrumentation:
        def __init__(self) -> None:
            self.counters: list[tuple[str, int, dict[str, str] | None]] = []

        def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
            self.counters.append((name, value, attrs))

    capture = _CaptureInstrumentation()
    monkeypatch.setattr(
        "btcbot.services.execution_service_stage4.get_instrumentation",
        lambda: capture,
    )

    exchange = FakeExchangeStage4()
    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=Settings(DRY_RUN=False, KILL_SWITCH=False, LIVE_TRADING=True, SAFE_MODE=False, LIVE_TRADING_ACK="I_UNDERSTAND", BTCTURK_API_KEY="key", BTCTURK_API_SECRET="secret", RISK_MAX_ORDER_NOTIONAL_TRY="50"),
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1"),
        reason="contract",
        client_order_id="cid-cap-notional",
    )

    report = svc.execute_with_report([action])

    assert report.submitted == 0
    assert report.rejected == 1
    assert exchange.submits == []
    rejected = store.get_stage4_order_by_client_id("cid-cap-notional")
    assert rejected is not None
    assert rejected.status == "rejected"
    with store._connect() as conn:
        last_error = conn.execute(
            "SELECT last_error FROM stage4_orders WHERE client_order_id = ?",
            ("cid-cap-notional",),
        ).fetchone()["last_error"]
    assert last_error == "max_order_notional_try"
    assert any(name == "stage4_cap_reject_total" for name, _, _ in capture.counters)


def test_execution_service_max_order_notional_guard_disabled_when_non_positive(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _CaptureInstrumentation:
        def __init__(self) -> None:
            self.counters: list[tuple[str, int, dict[str, str] | None]] = []

        def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
            self.counters.append((name, value, attrs))

    capture = _CaptureInstrumentation()
    monkeypatch.setattr(
        "btcbot.services.execution_service_stage4.get_instrumentation",
        lambda: capture,
    )

    exchange = FakeExchangeStage4()
    settings = Settings(
        DRY_RUN=False,
        KILL_SWITCH=False,
        LIVE_TRADING=True,
        SAFE_MODE=False,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        BTCTURK_API_KEY="key",
        BTCTURK_API_SECRET="secret",
    )
    object.__setattr__(settings, "risk_max_order_notional_try", Decimal("0"))

    svc = ExecutionService(
        exchange=exchange,
        state_store=store,
        settings=settings,
        rules_service=ExchangeRulesService(exchange),
    )
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("123.4"),
        qty=Decimal("1"),
        reason="contract",
        client_order_id="cid-cap-disabled",
    )

    report = svc.execute_with_report([action])

    assert report.submitted == 1
    assert report.rejected == 0
    assert len(exchange.submits) == 1
    assert not any(name == "stage4_cap_reject_total" for name, _, _ in capture.counters)
