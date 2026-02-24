from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from btcbot.adapters.exchange import ExchangeClient
from btcbot.config import Settings
from btcbot.domain.intent import Intent, to_order_intent
from btcbot.domain.models import (
    Balance,
    ExchangeError,
    ExchangeOrderStatus,
    OpenOrders,
    Order,
    OrderIntent,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    PairInfo,
    SymbolRules,
)
from btcbot.services import execution_service as execution_service_module
from btcbot.services import state_store as state_store_module
from btcbot.services.execution_service import ExecutionService, LiveTradingNotArmedError
from btcbot.services.state_store import StateStore


class RecordingExchange(ExchangeClient):
    def __init__(
        self, orders: list[Order] | None = None, balances: list[Balance] | None = None
    ) -> None:
        self.open_orders = orders or []
        self.balances = balances or [
            Balance(asset="TRY", free=Decimal("1000000")),
            Balance(asset="BTC", free=Decimal("100")),
            Balance(asset="ETH", free=Decimal("100")),
            Balance(asset="SOL", free=Decimal("1000")),
            Balance(asset="ADA", free=Decimal("100000")),
        ]
        self.canceled: list[str] = []
        self.placed: list[tuple[str, OrderSide, float, float, str]] = []
        self.get_balances_calls = 0
        self.get_open_orders_calls = 0

    def get_balances(self) -> list[Balance]:
        self.get_balances_calls += 1
        return list(self.balances)

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del symbol, limit
        return (0.0, 0.0)

    def get_exchange_info(self) -> list[PairInfo]:
        return []

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        del pair_symbol
        self.get_open_orders_calls += 1
        return OpenOrders(bids=[], asks=[])

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int) -> list[OrderSnapshot]:
        del pair_symbol, start_ms, end_ms
        return []

    def get_order(self, order_id: str) -> OrderSnapshot:
        raise ValueError(order_id)

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ) -> Order:
        self.placed.append((symbol, side, price, quantity, client_order_id or ""))
        return Order(
            order_id=f"oid-{len(self.placed)}",
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            status=OrderStatus.OPEN,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def cancel_order(self, order_id: str) -> bool:
        self.canceled.append(order_id)
        return True

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol:
            return [order for order in self.open_orders if order.symbol == symbol]
        return list(self.open_orders)


class CancelFailExchange(RecordingExchange):
    def __init__(self, orders: list[Order] | None = None) -> None:
        super().__init__(orders=orders)
        self.cancel_attempts = 0

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_attempts += 1
        raise RuntimeError(f"cancel-failed:{order_id}")


class FakeMarketDataService:
    def get_symbol_rules(self, pair_symbol: str) -> SymbolRules:
        assert pair_symbol in {"BTCTRY", "ETHTRY", "SOLTRY", "BTCUSDT"}
        return SymbolRules(
            pair_symbol=pair_symbol,
            price_scale=2,
            quantity_scale=4,
            min_total=None,
        )


class EmptyBalanceExchange(RecordingExchange):
    def get_balances(self) -> list[Balance]:
        self.get_balances_calls += 1
        return []


class AckOnlyExchange(RecordingExchange):
    def submit_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ):
        self.placed.append((symbol, side, price, quantity, client_order_id or ""))
        return type("OrderAck", (), {"exchange_order_id": "ack-200-1"})()


class KnownRejectExchange(RecordingExchange):
    def submit_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ):
        del symbol, side, price, quantity, client_order_id
        raise ExchangeError(
            "submit rejected",
            status_code=400,
            error_code=1123,
            error_message="FAILED_MIN_TOTAL_AMOUNT",
            request_method="POST",
            request_path="/api/v1/order",
            response_body='{"code":1123,"message":"FAILED_MIN_TOTAL_AMOUNT"}',
        )


def _intent(cycle_id: str = "c1") -> OrderIntent:
    return OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id=cycle_id,
    )


def test_kill_switch_logs_would_place_without_side_effects(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(exchange=exchange, state_store=store, dry_run=True, kill_switch=True)

    count = service.execute_intents([_intent()])

    assert count == 0
    assert exchange.placed == []
    assert "would place order" in caplog.text


def test_kill_switch_logs_would_cancel_without_side_effects(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    stale_order = Order(
        order_id="o1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
        updated_at=datetime.now(UTC),
    )

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange([stale_order])
    service = ExecutionService(exchange=exchange, state_store=store, dry_run=True, kill_switch=True)

    canceled = service.cancel_stale_orders(cycle_id="cycle-1")

    assert canceled == 0
    assert exchange.canceled == []
    assert "would cancel order" in caplog.text


def test_dry_run_cancel_stale_logs_would_cancel_without_side_effects(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    stale_order = Order(
        order_id="o2",
        symbol="ETH_TRY",
        side=OrderSide.BUY,
        price=200.0,
        quantity=0.2,
        status=OrderStatus.OPEN,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
        updated_at=datetime.now(UTC),
    )

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange([stale_order])
    service = ExecutionService(
        exchange=exchange, state_store=store, dry_run=True, kill_switch=False
    )

    canceled = service.cancel_stale_orders(cycle_id="cycle-2")

    assert canceled == 1
    assert exchange.canceled == []
    assert "would cancel stale order" in caplog.text


def test_sell_precheck_with_zero_base_balance_skips_exchange_submit(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange(balances=[Balance(asset="TRY", free=1000)])
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    intent = OrderIntent(
        symbol="ETH_TRY",
        side=OrderSide.SELL,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="sell-precheck",
    )
    placed = service.execute_intents([intent])

    assert placed == 0
    assert exchange.placed == []
    assert exchange.get_balances_calls == 1
    decision_records = [record for record in caplog.records if record.message == "decision_event"]
    assert decision_records
    assert any(
        record.__dict__.get("extra", {}).get("reason_code")
        == "execution_reject:insufficient_balance_precheck"
        for record in decision_records
    )
    precheck_records = [
        record
        for record in caplog.records
        if record.message == "execution_reject_insufficient_balance_precheck"
    ]
    assert precheck_records
    assert precheck_records[-1].__dict__.get("extra", {}).get("asset") == "ETH"
    assert Decimal(precheck_records[-1].__dict__.get("extra", {}).get("missing_amount", "0")) > 0
    summary = service.last_execute_summary
    assert summary["intents_rejected_precheck"] == 1
    assert summary["rejected_intents"] == 1
    assert summary["orders_failed_exchange"] == 0
    assert summary["attempted_exchange_calls"] == 0


def test_buy_precheck_with_insufficient_try_skips_exchange_submit(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange(balances=[Balance(asset="TRY", free=5)])
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent(cycle_id="buy-precheck")])

    assert placed == 0
    assert exchange.placed == []
    assert exchange.get_balances_calls == 1
    decision_records = [record for record in caplog.records if record.message == "decision_event"]
    assert decision_records
    assert any(
        record.__dict__.get("extra", {}).get("reason_code")
        == "execution_reject:insufficient_balance_precheck"
        for record in decision_records
    )
    precheck_records = [
        record
        for record in caplog.records
        if record.message == "execution_reject_insufficient_balance_precheck"
    ]
    assert precheck_records
    assert precheck_records[-1].__dict__.get("extra", {}).get("asset") == "TRY"
    assert Decimal(precheck_records[-1].__dict__.get("extra", {}).get("missing_amount", "0")) > 0
    summary = service.last_execute_summary
    assert summary["intents_rejected_precheck"] == 1
    assert summary["rejected_intents"] == 1
    assert summary["orders_failed_exchange"] == 0
    assert summary["attempted_exchange_calls"] == 0


def test_buy_precheck_derives_quote_asset_from_symbol(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange(
        balances=[
            Balance(asset="USDT", free=Decimal("1")),
            Balance(asset="TRY", free=Decimal("100000")),
        ]
    )
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents(
        [
            OrderIntent(
                symbol="BTC_USDT",
                side=OrderSide.BUY,
                price=100.0,
                quantity=0.1,
                notional=10.0,
                cycle_id="buy-usdt-precheck",
            )
        ]
    )

    assert placed == 0
    assert exchange.placed == []
    precheck_records = [
        record
        for record in caplog.records
        if record.message == "execution_reject_insufficient_balance_precheck"
    ]
    assert precheck_records
    assert precheck_records[-1].__dict__.get("extra", {}).get("asset") == "USDT"
    decision_records = [record for record in caplog.records if record.message == "decision_event"]
    assert any(
        record.__dict__.get("extra", {}).get("reason_code")
        == "execution_reject:insufficient_balance_precheck"
        for record in decision_records
    )


def test_insufficient_balance_does_not_create_idempotency_or_actions(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange(balances=[Balance(asset="TRY", free=1)])
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    intents = [_intent(cycle_id="c-a"), _intent(cycle_id="c-b")]
    assert service.execute_intents(intents) == 0
    assert exchange.get_balances_calls == 1

    with store._connect() as conn:
        action_count = conn.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
        idem_count = conn.execute(
            "SELECT COUNT(*) AS c FROM idempotency_keys WHERE action_type='place_order'"
        ).fetchone()["c"]

    assert action_count == 0
    assert idem_count == 0
    summary = service.last_execute_summary
    assert summary["intents_rejected_precheck"] == 2
    assert summary["rejected_intents"] == 2
    assert summary["orders_failed_exchange"] == 0
    assert summary["attempted_exchange_calls"] == 0


def test_check_balance_precondition_sell_missing_balances_fails_closed(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = ExecutionService(
        exchange=RecordingExchange(),
        state_store=store,
        spot_sell_requires_inventory=True,
    )
    service.sell_fee_in_base_bps = Decimal("10")

    is_sufficient, asset, required, available = service._check_balance_precondition(
        balances=None,
        symbol="ETH_TRY",
        side=OrderSide.SELL,
        price=Decimal("100"),
        quantity=Decimal("0.5"),
    )

    assert is_sufficient is False
    assert asset == "ETH"
    assert available == Decimal("0")
    assert required is not None and required > Decimal("0.5")


def test_check_balance_precondition_sell_insufficient_inventory(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = ExecutionService(exchange=RecordingExchange(), state_store=store)

    is_sufficient, asset, required, available = service._check_balance_precondition(
        balances={"ETH": Decimal("0.05")},
        symbol="ETH_TRY",
        side=OrderSide.SELL,
        price=Decimal("100"),
        quantity=Decimal("0.1"),
    )

    assert is_sufficient is False
    assert asset == "ETH"
    assert required == Decimal("0.1")
    assert available == Decimal("0.05")


def test_check_balance_precondition_sell_sufficient_inventory(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = ExecutionService(exchange=RecordingExchange(), state_store=store)

    is_sufficient, asset, required, available = service._check_balance_precondition(
        balances={"ETH": Decimal("1.2")},
        symbol="ETH_TRY",
        side=OrderSide.SELL,
        price=Decimal("100"),
        quantity=Decimal("0.1"),
    )

    assert is_sufficient is True
    assert asset is None
    assert required is None
    assert available is None


def test_check_balance_precondition_buy_missing_balances_remains_allowed(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = ExecutionService(exchange=RecordingExchange(), state_store=store)

    is_sufficient, asset, required, available = service._check_balance_precondition(
        balances=None,
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=Decimal("100"),
        quantity=Decimal("0.1"),
    )

    assert is_sufficient is True
    assert asset is None
    assert required is None
    assert available is None


def test_check_balance_precondition_sell_missing_balances_allowed_when_opt_out(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = ExecutionService(
        exchange=RecordingExchange(),
        state_store=store,
        spot_sell_requires_inventory=False,
    )

    for balances in (None, {}):
        is_sufficient, asset, required, available = service._check_balance_precondition(
            balances=balances,
            symbol="ETH_TRY",
            side=OrderSide.SELL,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
        )

        assert is_sufficient is True
        assert asset is None
        assert required is None
        assert available is None


def test_execute_intents_sell_rejects_when_balances_missing_with_reason(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = EmptyBalanceExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        spot_sell_requires_inventory=True,
    )

    intent = OrderIntent(
        symbol="ETH_TRY",
        side=OrderSide.SELL,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="sell-missing-balances",
    )

    placed = service.execute_intents([intent])

    assert placed == 0
    assert exchange.placed == []
    precheck_records = [
        record
        for record in caplog.records
        if record.message == "execution_reject_insufficient_balance_precheck"
    ]
    assert precheck_records
    assert (
        precheck_records[-1].__dict__.get("extra", {}).get("precheck_reason")
        == "balances_missing_fail_closed"
    )


def test_execute_intents_sell_missing_balances_not_rejected_when_opt_out(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = EmptyBalanceExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        spot_sell_requires_inventory=False,
    )

    intent = OrderIntent(
        symbol="ETH_TRY",
        side=OrderSide.SELL,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="sell-missing-balances-opt-out",
    )

    placed = service.execute_intents([intent])

    assert placed == 1
    precheck_records = [
        record
        for record in caplog.records
        if record.message == "execution_reject_insufficient_balance_precheck"
    ]
    assert precheck_records == []
    assert service.last_execute_summary["intents_rejected_precheck"] == 0


def test_live_mode_saves_order_state(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent(cycle_id="cycle-live")])

    assert placed == 1
    assert exchange.placed[0][:4] == ("BTCTRY", OrderSide.BUY, 100.0, Decimal("0.1000"))
    assert exchange.placed[0][4].startswith("meva2-")


def test_live_mode_marks_canceled_order(tmp_path) -> None:
    stale_order = Order(
        order_id="o-live",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
        updated_at=datetime.now(UTC),
    )
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange([stale_order])
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    canceled = service.cancel_stale_orders(cycle_id="cycle-cancel")

    assert canceled == 1
    assert exchange.canceled == ["o-live"]


def test_execute_intents_idempotent_for_same_payload(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    intent = _intent(cycle_id="cycle-idempotent")

    first = service.execute_intents([intent])
    second = service.execute_intents([intent])

    assert first == 1
    assert second == 0
    assert len(exchange.placed) == 1


def test_cancel_stale_orders_idempotent_for_same_order(tmp_path) -> None:
    stale_order = Order(
        order_id="o-dedupe",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
        updated_at=datetime.now(UTC),
    )
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange([stale_order])
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    first = service.cancel_stale_orders(cycle_id="cycle-cancel-dedupe")
    second = service.cancel_stale_orders(cycle_id="cycle-cancel-dedupe")

    assert first == 1
    assert second == 0
    assert exchange.canceled == ["o-dedupe"]


def test_live_mode_requires_arming(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=False,
    )

    with pytest.raises(LiveTradingNotArmedError):
        service._ensure_live_side_effects_allowed()


def _stage3_intent() -> Intent:
    return Intent.create(
        cycle_id="cycle-stage3",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        qty=Decimal("0.1"),
        limit_price=Decimal("100"),
        reason="test",
    )


def test_execute_intents_stage3_requires_cycle_id_for_intent(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange, state_store=store, dry_run=True, kill_switch=False
    )

    with pytest.raises(ValueError, match="cycle_id is required"):
        service.execute_intents([_stage3_intent()])


def test_execute_intents_stage3_uses_idempotency_key_for_dedupe(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange, state_store=store, dry_run=True, kill_switch=False
    )
    intent = _stage3_intent()

    first = service.execute_intents([intent], cycle_id="cycle-a")
    second = service.execute_intents([intent], cycle_id="cycle-a")

    assert first == 1
    assert second == 0
    with store._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, MIN(status) AS status FROM idempotency_keys WHERE action_type='place_order' AND key=?",
            (intent.idempotency_key,),
        ).fetchone()
    assert row is not None and row["c"] == 1
    assert row["status"] == "SIMULATED"


def test_dry_run_then_live_promotes_simulated_idempotency(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    intent = _stage3_intent()

    dry_exchange = RecordingExchange()
    dry_service = ExecutionService(
        exchange=dry_exchange,
        state_store=store,
        dry_run=True,
        kill_switch=False,
    )
    assert dry_service.execute_intents([intent], cycle_id="cycle-a") == 1

    with store._connect() as conn:
        dry_row = conn.execute(
            """
            SELECT status FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert dry_row is not None
    assert dry_row["status"] == "SIMULATED"

    live_exchange = RecordingExchange()
    live_service = ExecutionService(
        exchange=live_exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    assert live_service.execute_intents([intent], cycle_id="cycle-a") == 1
    assert len(live_exchange.placed) == 1

    with store._connect() as conn:
        live_row = conn.execute(
            """
            SELECT status FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert live_row is not None
    assert live_row["status"] == "COMMITTED"


def test_cancel_non_uncertain_failure_finalizes_failed_and_retries(tmp_path) -> None:
    stale_order = Order(
        order_id="o-fail-cancel",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
        updated_at=datetime.now(UTC),
    )
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = CancelFailExchange([stale_order])
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    assert service.cancel_stale_orders(cycle_id="cycle-fail-cancel") == 0
    with store._connect() as conn:
        first = conn.execute(
            """
            SELECT status FROM idempotency_keys
            WHERE action_type='cancel_order' AND key='cancel:o-fail-cancel'
            """
        ).fetchone()
    assert first is not None
    assert first["status"] == "FAILED"

    assert service.cancel_stale_orders(cycle_id="cycle-fail-cancel") == 0
    assert exchange.cancel_attempts == 2


def test_record_action_conflict_finalizes_idempotency_not_pending(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    intent = _stage3_intent()
    order_intent = to_order_intent(intent, cycle_id="cycle-a")
    payload_hash = service._place_hash(order_intent)
    dedupe_key = f"place_order:{intent.idempotency_key}"
    existing_action_id = store.record_action(
        "cycle-a",
        "place_order",
        payload_hash,
        dedupe_key=dedupe_key,
    )
    assert existing_action_id is not None

    assert service.execute_intents([intent], cycle_id="cycle-a") == 0
    assert exchange.placed == []
    with store._connect() as conn:
        row = conn.execute(
            """
            SELECT status, action_id FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "COMMITTED"
    assert row["action_id"] == existing_action_id


def test_place_order_idempotency_across_bucket_boundary(monkeypatch, tmp_path) -> None:
    class _T1:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 4, 59, tzinfo=UTC)

    class _T2:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 5, 1, tzinfo=UTC)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    intent = _stage3_intent()

    monkeypatch.setattr("btcbot.services.execution_service.datetime", _T1)
    first = service.execute_intents([intent], cycle_id="cycle-a")

    monkeypatch.setattr("btcbot.services.execution_service.datetime", _T2)
    second = service.execute_intents([intent], cycle_id="cycle-a")

    assert first == 1
    assert second == 0
    assert len(exchange.placed) == 1
    with store._connect() as conn:
        row = conn.execute(
            """
            SELECT status FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "COMMITTED"


def test_restart_safety_inflight_pending_blocks_second_submit(monkeypatch, tmp_path) -> None:
    class _Now:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(state_store_module, "datetime", _Now)
    monkeypatch.setattr(execution_service_module, "datetime", _Now)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    intent = _stage3_intent()
    payload_hash = service._place_hash(to_order_intent(intent, cycle_id="cycle-a"))
    store.reserve_idempotency_key(
        "place_order",
        intent.idempotency_key,
        payload_hash,
        ttl_seconds=7 * 24 * 60 * 60,
    )

    placed = service.execute_intents([intent], cycle_id="cycle-a")

    assert placed == 0
    assert exchange.placed == []


def test_restart_safety_stale_pending_without_client_order_id_recovers(
    monkeypatch, tmp_path
) -> None:
    class _T0:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    class _T1:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 2, 0, tzinfo=UTC)

    monkeypatch.setattr(state_store_module, "datetime", _T0)
    monkeypatch.setattr(execution_service_module, "datetime", _T0)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    intent = _stage3_intent()
    payload_hash = service._place_hash(to_order_intent(intent, cycle_id="cycle-a"))
    store.reserve_idempotency_key(
        "place_order",
        intent.idempotency_key,
        payload_hash,
        ttl_seconds=7 * 24 * 60 * 60,
    )

    monkeypatch.setattr(state_store_module, "datetime", _T1)
    monkeypatch.setattr(execution_service_module, "datetime", _T1)
    placed = service.execute_intents([intent], cycle_id="cycle-a")

    assert placed == 1
    assert len(exchange.placed) == 1


def test_restart_safety_stale_pending_with_client_order_id_reconciles_without_resubmit(
    monkeypatch, tmp_path
) -> None:
    class RecoveryExchange(RecordingExchange):
        def get_open_orders(self, pair_symbol: str) -> OpenOrders:
            del pair_symbol
            return OpenOrders(bids=[], asks=[])

        def get_all_orders(
            self, pair_symbol: str, start_ms: int, end_ms: int
        ) -> list[OrderSnapshot]:
            del pair_symbol, start_ms, end_ms
            return [
                OrderSnapshot(
                    order_id="oid-existing",
                    client_order_id="cid-stale",
                    pair_symbol="BTCTRY",
                    side=OrderSide.BUY,
                    price=100.0,
                    quantity=0.1,
                    timestamp=1_704_067_200_000,
                )
            ]

    class _T0:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    class _T1:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 2, 0, tzinfo=UTC)

    monkeypatch.setattr(state_store_module, "datetime", _T0)
    monkeypatch.setattr(execution_service_module, "datetime", _T0)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecoveryExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    intent = _stage3_intent()
    payload_hash = service._place_hash(to_order_intent(intent, cycle_id="cycle-a"))
    store.reserve_idempotency_key(
        "place_order",
        intent.idempotency_key,
        payload_hash,
        ttl_seconds=7 * 24 * 60 * 60,
    )
    store.finalize_idempotency_key(
        "place_order",
        intent.idempotency_key,
        action_id=None,
        client_order_id="cid-stale",
        order_id=None,
        status="PENDING",
    )

    monkeypatch.setattr(state_store_module, "datetime", _T1)
    monkeypatch.setattr(execution_service_module, "datetime", _T1)

    placed = service.execute_intents([intent], cycle_id="cycle-a")

    assert placed == 0
    assert exchange.placed == []
    with store._connect() as conn:
        row = conn.execute(
            """
            SELECT status, order_id FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "COMMITTED"
    assert row["order_id"] == "oid-existing"


def test_stale_pending_lookup_failure_backoff_and_eventual_failed(monkeypatch, tmp_path) -> None:
    class FailingRecoveryExchange(RecordingExchange):
        def __init__(self) -> None:
            super().__init__()
            self.lookup_calls = 0

        def get_open_orders(self, pair_symbol: str) -> OpenOrders:
            del pair_symbol
            return OpenOrders(bids=[], asks=[])

        def get_all_orders(
            self, pair_symbol: str, start_ms: int, end_ms: int
        ) -> list[OrderSnapshot]:
            del pair_symbol, start_ms, end_ms
            self.lookup_calls += 1
            raise RuntimeError("network-timeout")

    class _Clock:
        current = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

        @classmethod
        def now(cls, tz):
            del tz
            return cls.current

    monkeypatch.setattr(state_store_module, "datetime", _Clock)
    monkeypatch.setattr(execution_service_module, "datetime", _Clock)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = FailingRecoveryExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        pending_recovery_max_attempts=3,
        pending_recovery_backoff_seconds=30,
    )
    intent = _stage3_intent()
    payload_hash = service._place_hash(to_order_intent(intent, cycle_id="cycle-a"))
    store.reserve_idempotency_key(
        "place_order",
        intent.idempotency_key,
        payload_hash,
        ttl_seconds=7 * 24 * 60 * 60,
    )
    store.finalize_idempotency_key(
        "place_order",
        intent.idempotency_key,
        action_id=None,
        client_order_id="cid-stale",
        order_id=None,
        status="PENDING",
    )

    # Attempt 1: stale, lookup fails, backoff is set.
    _Clock.current = datetime(2024, 1, 1, 0, 2, 0, tzinfo=UTC)
    assert service.execute_intents([intent], cycle_id="cycle-a") == 0
    assert exchange.lookup_calls == 1
    with store._connect() as conn:
        row1 = conn.execute(
            """
            SELECT status, recovery_attempts, next_recovery_at_epoch
            FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert row1 is not None
    assert row1["status"] == "PENDING"
    assert row1["recovery_attempts"] == 1
    assert row1["next_recovery_at_epoch"] == int(_Clock.current.timestamp()) + 30

    # Before next_recovery_at_epoch: should skip lookup.
    _Clock.current = datetime(2024, 1, 1, 0, 2, 10, tzinfo=UTC)
    assert service.execute_intents([intent], cycle_id="cycle-a") == 0
    assert exchange.lookup_calls == 1

    # Attempt 2 after backoff.
    _Clock.current = datetime(2024, 1, 1, 0, 2, 40, tzinfo=UTC)
    assert service.execute_intents([intent], cycle_id="cycle-a") == 0
    assert exchange.lookup_calls == 2
    with store._connect() as conn:
        row2 = conn.execute(
            """
            SELECT status, recovery_attempts, next_recovery_at_epoch
            FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert row2 is not None
    assert row2["status"] == "PENDING"
    assert row2["recovery_attempts"] == 2
    assert row2["next_recovery_at_epoch"] == int(_Clock.current.timestamp()) + 60

    # Attempt 3 reaches max attempts and transitions to FAILED.
    _Clock.current = datetime(2024, 1, 1, 0, 3, 50, tzinfo=UTC)
    assert service.execute_intents([intent], cycle_id="cycle-a") == 0
    assert exchange.lookup_calls == 3
    with store._connect() as conn:
        row3 = conn.execute(
            """
            SELECT status, recovery_attempts, next_recovery_at_epoch
            FROM idempotency_keys
            WHERE action_type='place_order' AND key=?
            """,
            (intent.idempotency_key,),
        ).fetchone()
    assert row3 is not None
    assert row3["status"] == "FAILED"
    assert row3["recovery_attempts"] == 3
    assert row3["next_recovery_at_epoch"] is None

    # Next cycle can re-reserve and submit once.
    _Clock.current = datetime(2024, 1, 1, 0, 4, 0, tzinfo=UTC)
    assert service.execute_intents([intent], cycle_id="cycle-a") == 1
    assert len(exchange.placed) == 1


def test_execute_intents_legacy_order_intent_uses_place_hash(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange, state_store=store, dry_run=True, kill_switch=False
    )
    intent = _intent(cycle_id="cycle-legacy")

    first = service.execute_intents([intent])
    second = service.execute_intents([intent])

    assert first == 1
    assert second == 0
    assert store.action_count("would_place_order", service._place_hash(intent)) == 1


class Submit400Exchange(RecordingExchange):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ) -> Order:
        del symbol, side, price, quantity, client_order_id
        self.attempts += 1
        raise ExchangeError(
            "bad request",
            status_code=400,
            error_code="1001",
            error_message="invalid qty",
            request_method="POST",
            request_path="/api/v1/order",
            request_json={"quantity": "0"},
            response_body='{"code":1001,"message":"invalid qty"}',
        )


def test_state_store_new_after_grace_not_blocking(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    old = datetime.now(UTC) - timedelta(seconds=130)
    store.save_order(
        Order(
            order_id="o-old-new",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=100.0,
            quantity=0.1,
            status=OrderStatus.NEW,
            created_at=old,
            updated_at=old,
        )
    )
    active = store.find_open_or_unknown_orders(["BTCTRY"], new_grace_seconds=60)
    assert active == []


def test_refresh_order_lifecycle_missing_transitions(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    now = datetime.now(UTC)
    store.save_order(
        Order(
            order_id="o-new",
            client_order_id="cid-new",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=100.0,
            quantity=0.1,
            status=OrderStatus.NEW,
            created_at=now - timedelta(seconds=120),
            updated_at=now - timedelta(seconds=120),
        )
    )
    store.save_order(
        Order(
            order_id="o-open",
            client_order_id="cid-open",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=100.0,
            quantity=0.1,
            status=OrderStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )
    store.save_order(
        Order(
            order_id="o-unk",
            client_order_id="cid-unk",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=100.0,
            quantity=0.1,
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )

    exchange = RecordingExchange()
    svc = ExecutionService(exchange=exchange, state_store=store)
    svc.refresh_order_lifecycle(["BTC_TRY"])

    rows = {
        o.order_id: o
        for o in store.find_open_or_unknown_orders(
            ["BTCTRY"], include_new_after_grace=True, include_escalated_unknown=True
        )
    }
    assert "o-open" not in rows
    assert rows["o-unk"].unknown_next_probe_at is not None
    with store._connect() as conn:
        rej = conn.execute(
            "SELECT status, exchange_status_raw FROM orders WHERE order_id='o-new'"
        ).fetchone()
    assert rej is not None
    assert rej["status"] == "rejected"
    assert "missing_on_exchange_after_grace" in str(rej["exchange_status_raw"])


def test_submit_400_logs_request_and_response(tmp_path, caplog) -> None:
    caplog.set_level("ERROR")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = ExecutionService(
        exchange=Submit400Exchange(),
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    placed = service.execute_intents([_intent(cycle_id="c-400")])
    assert placed == 0
    assert "exchange_submit_failed" in caplog.text
    payloads = [
        getattr(record, "extra", {})
        for record in caplog.records
        if record.message == "exchange_submit_failed"
    ]
    assert payloads
    assert payloads[-1].get("request_json") == {"quantity": "0"}
    assert "invalid qty" in str(payloads[-1].get("response_body"))


def test_submit_rejected_order_id_unique_per_failure(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = ExecutionService(
        exchange=Submit400Exchange(),
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    i1 = _intent(cycle_id="c-uniq-1")
    i2 = _intent(cycle_id="c-uniq-2")
    service.execute_intents([i1])
    service.execute_intents([i2])
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT order_id FROM orders WHERE status='rejected' ORDER BY created_at"
        ).fetchall()
    ids = [str(r["order_id"]) for r in rows]
    assert len(ids) >= 2
    assert len(set(ids)) == len(ids)


def test_refresh_order_lifecycle_throttles_symbols_without_local_orders(tmp_path) -> None:
    class CountingExchange(RecordingExchange):
        def __init__(self) -> None:
            super().__init__()
            self.open_orders_calls = 0

        def get_open_orders(self, pair_symbol: str) -> OpenOrders:
            del pair_symbol
            self.open_orders_calls += 1
            return OpenOrders(bids=[], asks=[])

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = CountingExchange()
    svc = ExecutionService(exchange=exchange, state_store=store)

    svc.refresh_order_lifecycle(["BTC_TRY"])
    svc.refresh_order_lifecycle(["BTC_TRY"])

    assert exchange.open_orders_calls == 1


def test_refresh_order_lifecycle_summary_includes_call_and_throttle_counts(
    tmp_path, caplog
) -> None:
    class CountingExchange(RecordingExchange):
        def __init__(self) -> None:
            super().__init__()
            self.open_orders_calls = 0

        def get_open_orders(self, pair_symbol: str) -> OpenOrders:
            del pair_symbol
            self.open_orders_calls += 1
            return OpenOrders(bids=[], asks=[])

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = CountingExchange()
    svc = ExecutionService(exchange=exchange, state_store=store)

    caplog.set_level("INFO")
    svc.refresh_order_lifecycle(["BTC_TRY"])
    svc.refresh_order_lifecycle(["BTC_TRY"])

    summary_records = [r for r in caplog.records if r.getMessage() == "order_reconcile_summary"]
    assert summary_records
    payload = getattr(summary_records[-1], "extra", {})
    assert "refresh_skipped_due_to_throttle_count" in payload
    assert "open_orders_calls_count" in payload
    assert "all_orders_calls_count" in payload


def test_reconcile_snapshot_missing_side_does_not_default_to_buy(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    svc = ExecutionService(exchange=exchange, state_store=store)

    snapshot = OrderSnapshot(
        order_id="missing-side-1",
        client_order_id="cid-missing-side-1",
        pair_symbol="BTCTRY",
        side=None,
        status=ExchangeOrderStatus.OPEN,
        status_raw="Open",
        price=Decimal("100"),
        quantity=Decimal("0.1"),
        update_time=int(datetime.now(UTC).timestamp() * 1000),
        timestamp=int(datetime.now(UTC).timestamp() * 1000),
    )
    svc._save_reconciled_snapshot(snapshot)

    with store._connect() as conn:
        row = conn.execute("SELECT side FROM orders WHERE order_id='missing-side-1'").fetchone()
    assert row is None


def test_place_order_idempotency_is_stable_across_cycles_but_changes_on_material_intent(
    tmp_path,
) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=True,
        kill_switch=False,
    )

    intent_a = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="cycle-a",
    )
    intent_b = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="cycle-b",
    )
    intent_c = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=101.0,
        quantity=0.1,
        notional=10.1,
        cycle_id="cycle-c",
    )

    key_a = service._stable_place_intent_hash(intent_a)
    key_b = service._stable_place_intent_hash(intent_b)
    key_c = service._stable_place_intent_hash(intent_c)

    assert key_a == key_b
    assert key_a != key_c


def test_settings_env_file_opt_in_only(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env.custom"
    env_file.write_text("TARGET_TRY=777\n", encoding="utf-8")

    monkeypatch.delenv("SETTINGS_ENV_FILE", raising=False)
    default_settings = Settings()
    assert default_settings.target_try != 777

    monkeypatch.setenv("SETTINGS_ENV_FILE", str(env_file))
    monkeypatch.setenv("TARGET_TRY", "999")
    opted_in = Settings()
    assert opted_in.target_try == 999


def test_exchange_submit_failure_increments_orders_failed_exchange_only_when_attempted(
    tmp_path,
) -> None:
    class SubmitFailExchange(RecordingExchange):
        def place_limit_order(
            self,
            symbol: str,
            side: OrderSide,
            price: float,
            quantity: float,
            client_order_id: str | None = None,
        ) -> Order:
            del symbol, side, price, quantity, client_order_id
            raise ExchangeError("submit failed", status_code=400)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = SubmitFailExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent(cycle_id="submit-fail")])

    assert placed == 0
    summary = service.last_execute_summary
    assert summary["attempted_exchange_calls"] == 1
    assert summary["orders_failed_exchange"] == 1
    assert summary["intents_rejected_precheck"] == 0
    assert summary["rejected_intents"] == 0


def test_execute_intents_skips_lifecycle_refresh_when_cycle_marked(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    service.mark_lifecycle_refreshed(cycle_id="cycle-lifecycle")
    placed = service.execute_intents([_intent(cycle_id="cycle-lifecycle")])

    assert placed == 1
    assert exchange.get_open_orders_calls == 0


def test_cycle_balance_cache_is_bounded(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(exchange=exchange, state_store=store, dry_run=True)

    for idx in range(20):
        service.prime_cycle_balances(
            cycle_id=f"cycle-{idx}",
            balances=[Balance(asset="TRY", free=Decimal(str(100 + idx)))],
        )

    assert len(service._cycle_balance_cache) == 10
    assert "cycle-0" not in service._cycle_balance_cache
    assert "cycle-19" in service._cycle_balance_cache


def test_submit_ack_mapped_to_domain_result_with_order_id(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = AckOnlyExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent(cycle_id="ack-map")])

    assert placed == 1
    saved = store.get_order("ack-200-1")
    assert saved is not None
    assert saved.order_id == "ack-200-1"


def test_known_min_total_reject_does_not_crash_pipeline(tmp_path, caplog) -> None:
    caplog.set_level("ERROR")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = KnownRejectExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent(cycle_id="known-reject")])

    assert placed == 0
    assert service.last_execute_summary["orders_failed_exchange"] == 1
    assert any(r.message == "exchange_submit_failed" for r in caplog.records)


def test_submission_guarded_by_policy_emits_log_and_metric(monkeypatch, caplog) -> None:
    class _StateStore:
        def get_kill_switch(self, _process_role):
            return (False, "", None)

    class _Exchange:
        def health_snapshot(self):
            return {"degraded": False, "breaker_open": False}

    class _Instrumentation:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def counter(self, name: str, value: int, attrs: dict[str, object] | None = None) -> None:
            del value, attrs
            self.calls.append(name)

    inst = _Instrumentation()
    monkeypatch.setattr(execution_service_module, "is_trading_blocked_by_policy", lambda: True)
    monkeypatch.setattr(execution_service_module, "get_instrumentation", lambda: inst)

    service = ExecutionService.__new__(ExecutionService)
    service.dry_run = False
    service.state_store = _StateStore()
    service.process_role = "MONITOR"
    service.exchange = _Exchange()

    with caplog.at_level("WARNING"):
        guarded = service._submission_guarded_by_runtime_state()

    assert guarded is True
    assert "trading_blocked_by_policy_total" in inst.calls
    assert any("submission_blocked_by_policy_rotation_hygiene" in rec.message for rec in caplog.records)
