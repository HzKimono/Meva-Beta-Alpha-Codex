from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import btcbot.services.execution_service as execution_service_module
from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import (
    Balance,
    ExchangeError,
    ExchangeOrderStatus,
    OpenOrderItem,
    OpenOrders,
    Order,
    OrderIntent,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    PairInfo,
    SymbolRules,
)
from btcbot.services.execution_service import ExecutionService, SubmitBlockedDueToUnknownError
from btcbot.services.state_store import StateStore


class FakeMarketDataService:
    def get_symbol_rules(self, pair_symbol: str) -> SymbolRules:
        return SymbolRules(pair_symbol=pair_symbol, price_scale=2, quantity_scale=4)


class LifecycleExchange(ExchangeClient):
    def __init__(self) -> None:
        self.place_calls = 0
        self.cancel_calls = 0
        self.place_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.open_snapshots: list[OrderSnapshot] = []
        self.all_snapshots: list[OrderSnapshot] = []
        self.get_all_orders_calls = 0
        self.last_start_ms: int | None = None
        self.last_end_ms: int | None = None

    def get_balances(self) -> list[Balance]:
        return []

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[Decimal, Decimal]:
        del symbol, limit
        return (Decimal("0"), Decimal("0"))

    def get_exchange_info(self) -> list[PairInfo]:
        return []

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        bids: list[OpenOrderItem] = []
        asks: list[OpenOrderItem] = []
        for snapshot in self.open_snapshots:
            if snapshot.pair_symbol != pair_symbol:
                continue
            item = OpenOrderItem(
                id=int(snapshot.order_id),
                price=snapshot.price,
                amount=snapshot.quantity,
                quantity=snapshot.quantity,
                pairSymbol=snapshot.pair_symbol,
                pairSymbolNormalized="BTC_TRY",
                type="limit",
                method=snapshot.side.value if snapshot.side else "",
                orderClientId=snapshot.client_order_id,
                time=snapshot.timestamp,
                updateTime=snapshot.update_time,
                status="Untouched",
            )
            if snapshot.side == OrderSide.SELL:
                asks.append(item)
            else:
                bids.append(item)
        return OpenOrders(bids=bids, asks=asks)

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int) -> list[OrderSnapshot]:
        self.get_all_orders_calls += 1
        self.last_start_ms = start_ms
        self.last_end_ms = end_ms
        return [snapshot for snapshot in self.all_snapshots if snapshot.pair_symbol == pair_symbol]

    def get_order(self, order_id: str) -> OrderSnapshot:
        for snapshot in self.all_snapshots:
            if snapshot.order_id == order_id:
                return snapshot
        raise ValueError(order_id)

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> Order:
        self.place_calls += 1
        if self.place_error is not None:
            raise self.place_error
        now = datetime.now(UTC)
        return Order(
            order_id="101",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            status=OrderStatus.NEW,
            created_at=now,
            updated_at=now,
        )

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error
        return True

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        del symbol
        now = datetime.now(UTC)
        return [
            Order(
                order_id="101",
                client_order_id="cid-101",
                symbol="BTCTRY",
                side=OrderSide.BUY,
                price=Decimal("100.0"),
                quantity=Decimal("0.1"),
                status=OrderStatus.OPEN,
                created_at=now - timedelta(seconds=500),
                updated_at=now,
            )
        ]


def _intent(cycle_id: str = "c1") -> OrderIntent:
    return OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=Decimal("100.0"),
        quantity=Decimal("0.1"),
        notional=Decimal("10.0"),
        cycle_id=cycle_id,
    )


def _service(tmp_path, exchange: LifecycleExchange) -> ExecutionService:
    return ExecutionService(
        exchange=exchange,
        state_store=StateStore(str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        ttl_seconds=1,
    )


def test_refresh_lifecycle_marks_reconciled_from_open_orders(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="101",
            client_order_id="cid-open",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )

    exchange.open_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id="cid-open",
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.OPEN,
            timestamp=1700000000000,
            update_time=1700000000100,
            status_raw="Open",
        )
    ]

    service.refresh_order_lifecycle(["BTC_TRY"])

    with service.state_store._connect() as conn:
        row = conn.execute(
            """
            SELECT status, exchange_status_raw, reconciled, last_seen_at
            FROM orders
            WHERE order_id = ?
            """,
            ("101",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "open"
    assert row["exchange_status_raw"] == "Untouched"
    assert row["reconciled"] == 1
    assert row["last_seen_at"] == 1700000000100


def test_refresh_lifecycle_marks_reconciled_from_all_orders(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="101",
            client_order_id="cid-cancel",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )

    exchange.open_snapshots = []
    exchange.all_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id="cid-cancel",
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.CANCELED,
            timestamp=1700000000000,
            update_time=1700000000100,
            status_raw="Canceled",
        )
    ]

    service.refresh_order_lifecycle(["BTC_TRY"])

    with service.state_store._connect() as conn:
        row = conn.execute(
            """
            SELECT status, exchange_status_raw, reconciled, last_seen_at
            FROM orders
            WHERE order_id = ?
            """,
            ("101",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "canceled"
    assert row["exchange_status_raw"] == "Canceled"
    assert row["reconciled"] == 1
    assert row["last_seen_at"] == 1700000000100


def test_lifecycle_new_to_open_on_refresh(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    placed = service.execute_intents([_intent("open")])
    assert placed == 1

    exchange.open_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id=None,
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.OPEN,
            timestamp=1700000000000,
            update_time=1700000000100,
        )
    ]
    service.refresh_order_lifecycle(["BTC_TRY"])

    stored = service.state_store.find_open_or_unknown_orders(["BTCTRY"])
    assert stored[0].status == OrderStatus.OPEN


def test_lifecycle_open_to_filled_from_all_orders(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    service.execute_intents([_intent("filled")])

    exchange.open_snapshots = []
    exchange.all_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id=None,
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.FILLED,
            timestamp=1700000000000,
            update_time=1700000000100,
            status_raw="Filled",
        )
    ]
    service.refresh_order_lifecycle(["BTC_TRY"])

    stored = service.state_store.find_open_or_unknown_orders(["BTCTRY"])
    assert stored == []


def test_lifecycle_open_to_canceled_after_confirmed_cancel(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    service.execute_intents([_intent("cancel")])

    exchange.cancel_error = ExchangeError("status=500")
    exchange.open_snapshots = []
    exchange.all_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id="cid-101",
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.CANCELED,
            timestamp=1700000000000,
            update_time=1700000000100,
            status_raw="Canceled",
        )
    ]

    canceled = service.cancel_stale_orders("cycle-cancel")
    assert canceled == 1


def test_unknown_order_reprobe_survives_restart_and_resolves_without_duplicate_submit(
    tmp_path,
) -> None:
    db_path = tmp_path / "state.db"

    exchange = LifecycleExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(str(db_path)),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        unknown_reprobe_initial_seconds=60,
        unknown_reprobe_max_seconds=60,
        unknown_reprobe_escalation_attempts=5,
    )
    intent = _intent("cycle-crash")
    assert service.execute_intents([intent]) == 1

    service.state_store.update_order_status(
        order_id="101", status=OrderStatus.UNKNOWN, reconciled=True
    )

    restarted = ExecutionService(
        exchange=exchange,
        state_store=StateStore(str(db_path)),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        unknown_reprobe_initial_seconds=60,
        unknown_reprobe_max_seconds=60,
        unknown_reprobe_escalation_attempts=5,
    )

    assert restarted.execute_intents([intent]) == 0
    assert exchange.place_calls == 1

    restarted.refresh_order_lifecycle(["BTC_TRY"])
    assert exchange.get_all_orders_calls == 1

    restarted.refresh_order_lifecycle(["BTC_TRY"])
    assert exchange.get_all_orders_calls == 1

    with restarted.state_store._connect() as conn:
        conn.execute("UPDATE orders SET unknown_next_probe_at = 0 WHERE order_id = ?", ("101",))

    exchange.all_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id=None,
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.FILLED,
            timestamp=1700000000000,
            update_time=1700000000100,
            status_raw="Filled",
        )
    ]

    restarted.refresh_order_lifecycle(["BTC_TRY"])
    with restarted.state_store._connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE order_id = ?", ("101",)).fetchone()

    assert row is not None
    assert row["status"] == "filled"


def test_unknown_probe_dynamic_lookback_uses_first_seen(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="101",
            client_order_id="cid-101",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )
    service.state_store.update_order_status(
        order_id="101",
        status=OrderStatus.UNKNOWN,
        reconciled=True,
    )

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    first_seen_ms = now_ms - 2 * 60 * 60 * 1000
    with service.state_store._connect() as conn:
        conn.execute(
            """
            UPDATE orders
            SET unknown_first_seen_at = ?, unknown_next_probe_at = 0
            WHERE order_id = ?
            """,
            (first_seen_ms, "101"),
        )

    service.refresh_order_lifecycle(["BTC_TRY"])

    assert exchange.get_all_orders_calls == 1
    assert exchange.last_start_ms is not None
    assert exchange.last_end_ms is not None
    assert exchange.last_start_ms <= first_seen_ms
    assert abs(exchange.last_end_ms - now_ms) < 30_000


def test_unknown_reprobe_clamps_corrupted_attempts(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        unknown_reprobe_initial_seconds=1,
        unknown_reprobe_max_seconds=60,
    )
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="101",
            client_order_id="cid-101",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )
    with service.state_store._connect() as conn:
        conn.execute(
            """
            UPDATE orders
            SET unknown_probe_attempts = ?, unknown_next_probe_at = 0
            WHERE order_id = ?
            """,
            (10**12, "101"),
        )

    service.refresh_order_lifecycle(["BTC_TRY"])

    with service.state_store._connect() as conn:
        row = conn.execute(
            "SELECT unknown_next_probe_at, unknown_last_probe_at FROM orders WHERE order_id = ?",
            ("101",),
        ).fetchone()

    assert row is not None
    assert row["unknown_last_probe_at"] is not None
    assert row["unknown_next_probe_at"] is not None


def test_unknown_escalation_emits_metric_and_forces_observe_only(tmp_path, monkeypatch) -> None:
    class FakeInstrumentation:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | float, dict | None]] = []

        def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
            self.calls.append((name, value, attrs))

        def gauge(self, name: str, value: float, *, attrs=None) -> None:
            self.calls.append((name, value, attrs))

    fake_metrics = FakeInstrumentation()
    monkeypatch.setattr(execution_service_module, "get_instrumentation", lambda: fake_metrics)

    exchange = LifecycleExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        unknown_reprobe_escalation_attempts=1,
        unknown_reprobe_force_observe_only=True,
    )
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="101",
            client_order_id="cid-101",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )
    with service.state_store._connect() as conn:
        conn.execute("UPDATE orders SET unknown_next_probe_at = 0 WHERE order_id = ?", ("101",))

    service.refresh_order_lifecycle(["BTC_TRY"])

    assert service.safe_mode is True
    metric_names = [name for name, _value, _attrs in fake_metrics.calls]
    assert "unknown_order_retry_escalations_total" in metric_names


def test_unknown_state_preserved_and_not_reacted(tmp_path) -> None:
    class NoOpenExchange(LifecycleExchange):
        def list_open_orders(self, symbol: str | None = None) -> list[Order]:
            del symbol
            return []

    exchange = NoOpenExchange()
    service = _service(tmp_path, exchange)
    service.state_store.save_order(
        Order(
            order_id="101",
            client_order_id="cid-101",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.UNKNOWN,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    assert service.cancel_stale_orders("cycle-unknown") == 0


def test_submit_uncertain_sets_metadata_unknown(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    exchange.place_error = ExchangeError("status=500")

    placed = service.execute_intents([_intent("uncertain")])
    assert placed == 0

    payload_hash = service._place_hash(_intent("uncertain"))
    row = service.state_store.get_latest_action("place_order", payload_hash)
    metadata = json.loads(row["metadata_json"])
    assert metadata["reconcile_status"] in {"unknown", "not_found"}


def test_submit_blocked_when_unknown_present_and_cancel_allowed(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="unknown:cid-1",
            client_order_id="cid-1",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )
    with pytest.raises(SubmitBlockedDueToUnknownError):
        service.execute_intents([_intent("freeze")])

    open_order = Order(
        order_id="known-open",
        client_order_id="cid-open",
        symbol="BTCTRY",
        side=OrderSide.BUY,
        price=Decimal("100.0"),
        quantity=Decimal("0.1"),
        status=OrderStatus.OPEN,
        created_at=now - timedelta(seconds=1000),
        updated_at=now - timedelta(seconds=1000),
    )
    exchange.list_open_orders = lambda symbol=None: [open_order]
    assert service.cancel_stale_orders("cycle-cancel-allowed") == 1


def test_freeze_remains_active_when_reconcile_fails(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="unknown:cid-1",
            client_order_id="cid-1",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )
    exchange.open_snapshots = []
    exchange.get_open_orders = lambda pair_symbol: (_ for _ in ()).throw(ExchangeError("status=429"))

    service.refresh_order_lifecycle(["BTC_TRY"])

    with pytest.raises(SubmitBlockedDueToUnknownError):
        service.execute_intents([_intent("freeze-still-on")])


def test_freeze_clears_only_after_reconcile_confirms_zero_unknown(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    exchange.place_error = ExchangeError("status=500")

    assert service.execute_intents([_intent("uncertain-path")]) == 0
    blocked_intent = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=Decimal("100.5"),
        quantity=Decimal("0.15"),
        notional=Decimal("15.075"),
        cycle_id="blocked-after-uncertain",
    )
    with pytest.raises(SubmitBlockedDueToUnknownError):
        service.execute_intents([blocked_intent])

    exchange.place_error = None
    unknown_orders = service.state_store.list_unknown_orders()
    assert len(unknown_orders) == 1
    unknown_client_order_id = unknown_orders[0].client_order_id
    with service.state_store._connect() as conn:
        conn.execute("UPDATE orders SET unknown_next_probe_at = 0")

    exchange.all_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id=unknown_client_order_id,
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.FILLED,
            timestamp=1700000000000,
            update_time=1700000000100,
            status_raw="Filled",
        )
    ]
    service.refresh_order_lifecycle(["BTC_TRY"])

    allowed_intent = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=Decimal("101.0"),
        quantity=Decimal("0.2"),
        notional=Decimal("20.2"),
        cycle_id="allowed-after-reconcile",
    )
    assert service.execute_intents([allowed_intent]) == 1



def test_submit_uses_single_chokepoint_callsite() -> None:
    source = Path("src/btcbot/services/execution_service.py").read_text()
    assert source.count("self.exchange.place_limit_order(") == 1
    assert "def _submit_limit_order(" in source


def test_state_store_persists_decimal_strings_without_float_coercion(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    now = datetime.now(UTC)
    store.save_order(
        Order(
            order_id="decimal-1",
            client_order_id="cid-decimal",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100.0100"),
            quantity=Decimal("0.1000"),
            status=OrderStatus.NEW,
            created_at=now,
            updated_at=now,
        )
    )
    with store._connect() as conn:
        row = conn.execute("SELECT price, qty FROM orders WHERE order_id = ?", ("decimal-1",)).fetchone()
    assert row is not None
    assert row["price"] == "100.0100"
    assert row["qty"] == "0.1000"


def test_save_order_rejects_float_values(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    now = datetime.now(UTC)
    with pytest.raises(TypeError):
        store.save_order(
            Order(
                order_id="bad-float",
                client_order_id="cid-bad",
                symbol="BTCTRY",
                side=OrderSide.BUY,
                price=Decimal("1"),
                quantity=Decimal("1"),
                status=OrderStatus.NEW,
                created_at=now,
                updated_at=now,
            ).model_copy(update={"price": 1.23})
        )


def test_reconcile_handles_decimal_precision_without_false_unknown(tmp_path) -> None:
    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="unknown:cid-precise",
            client_order_id="cid-precise",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100.0100"),
            quantity=Decimal("0.1000"),
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )
    with service.state_store._connect() as conn:
        conn.execute("UPDATE orders SET unknown_next_probe_at = 0 WHERE order_id = ?", ("unknown:cid-precise",))

    exchange.all_snapshots = [
        OrderSnapshot(
            order_id="101",
            client_order_id="cid-precise",
            pair_symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100.01"),
            quantity=Decimal("0.1"),
            status=ExchangeOrderStatus.FILLED,
            timestamp=1700000000000,
            update_time=1700000000100,
            status_raw="Filled",
        )
    ]

    service.refresh_order_lifecycle(["BTC_TRY"])
    assert service.unknown_order_registry.has_unknown() is False

    stored = service.state_store.get_order("101")
    assert stored is not None
    assert stored.price == Decimal("100.01")
    assert stored.quantity == Decimal("0.1")


def test_submit_gate_enforced_metric_emitted_on_block_and_allow(tmp_path, monkeypatch) -> None:
    class FakeInstrumentation:
        def __init__(self) -> None:
            self.counters: list[str] = []

        def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
            del value, attrs
            self.counters.append(name)

        def gauge(self, name: str, value: float, *, attrs=None) -> None:
            del name, value, attrs

    fake_metrics = FakeInstrumentation()
    monkeypatch.setattr(execution_service_module, "get_instrumentation", lambda: fake_metrics)

    exchange = LifecycleExchange()
    service = _service(tmp_path, exchange)
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="unknown:metric",
            client_order_id="cid-metric",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )

    with pytest.raises(SubmitBlockedDueToUnknownError):
        service.execute_intents([_intent("metric-block")])

    service.state_store.update_order_status(
        order_id="unknown:metric",
        status=OrderStatus.FILLED,
        reconciled=True,
    )
    service.execute_intents([OrderIntent(symbol="BTC_TRY", side=OrderSide.BUY, price=Decimal("101"), quantity=Decimal("0.2"), notional=Decimal("20.2"), cycle_id="metric-allow")])

    assert fake_metrics.counters.count("submit_gate_enforced_total") >= 2

