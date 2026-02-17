from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.intent import Intent, to_order_intent
from btcbot.domain.models import (
    Balance,
    OpenOrders,
    Order,
    OrderIntent,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    PairInfo,
    SymbolRules,
)
from btcbot.services.execution_service import ExecutionService, LiveTradingNotArmedError
from btcbot.services.state_store import StateStore


class RecordingExchange(ExchangeClient):
    def __init__(self, orders: list[Order] | None = None) -> None:
        self.open_orders = orders or []
        self.canceled: list[str] = []
        self.placed: list[tuple[str, OrderSide, float, float, str]] = []

    def get_balances(self) -> list[Balance]:
        return []

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del symbol, limit
        return (0.0, 0.0)

    def get_exchange_info(self) -> list[PairInfo]:
        return []

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        del pair_symbol
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
        assert pair_symbol in {"BTCTRY", "ETHTRY", "SOLTRY"}
        return SymbolRules(
            pair_symbol=pair_symbol,
            price_scale=2,
            quantity_scale=4,
            min_total=None,
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
    )

    placed = service.execute_intents([_intent(cycle_id="cycle-live")])

    assert placed == 1
    assert exchange.placed[0][:4] == ("BTCTRY", OrderSide.BUY, 100.0, 0.1)
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


def test_restart_safety_pending_reservation_blocks_second_submit(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
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
