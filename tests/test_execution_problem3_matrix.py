from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

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
from btcbot.services.execution_service import ExecutionService
from btcbot.services.state_store import StateStore


class FakeMarketDataService:
    def get_symbol_rules(self, pair_symbol: str) -> SymbolRules:
        return SymbolRules(pair_symbol=pair_symbol, price_scale=2, quantity_scale=4)


class ScriptedExchange(ExchangeClient):
    def __init__(self) -> None:
        self.place_plan: deque[object] = deque()
        self.cancel_plan: deque[object] = deque()
        self.place_calls = 0
        self.cancel_calls = 0
        self.orders_by_id: dict[str, OrderSnapshot] = {}
        self.order_seq = 100

    def get_balances(self) -> list[Balance]:
        return []

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del symbol, limit
        return (0.0, 0.0)

    def get_exchange_info(self) -> list[PairInfo]:
        return []

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        bids: list[OpenOrderItem] = []
        asks: list[OpenOrderItem] = []
        for snapshot in self.orders_by_id.values():
            if snapshot.pair_symbol != pair_symbol:
                continue
            if snapshot.status not in {ExchangeOrderStatus.OPEN, ExchangeOrderStatus.PARTIAL}:
                continue
            item = OpenOrderItem(
                id=int(snapshot.order_id),
                price=snapshot.price,
                amount=snapshot.quantity,
                quantity=snapshot.quantity,
                pairSymbol=snapshot.pair_symbol,
                pairSymbolNormalized=snapshot.pair_symbol,
                type="limit",
                method=snapshot.side.value,
                orderClientId=snapshot.client_order_id,
                time=snapshot.timestamp,
                updateTime=snapshot.update_time,
                status="Untouched",
            )
            if snapshot.side == OrderSide.BUY:
                bids.append(item)
            else:
                asks.append(item)
        return OpenOrders(bids=bids, asks=asks)

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int) -> list[OrderSnapshot]:
        del start_ms, end_ms
        return [
            snapshot
            for snapshot in self.orders_by_id.values()
            if snapshot.pair_symbol == pair_symbol
        ]

    def get_order(self, order_id: str) -> OrderSnapshot:
        return self.orders_by_id[order_id]

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ) -> Order:
        self.place_calls += 1
        if self.place_plan:
            event = self.place_plan.popleft()
            if isinstance(event, Exception):
                if event.args and event.args[0] == "create_then_raise":
                    self._create_open(symbol, side, price, quantity, client_order_id)
                    raise httpx.TimeoutException("timeout after send")
                raise event
        now = datetime.now(UTC)
        order_id = self._create_open(symbol, side, price, quantity, client_order_id)
        return Order(
            order_id=order_id,
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
        if self.cancel_plan:
            event = self.cancel_plan.popleft()
            if isinstance(event, Exception):
                if event.args and event.args[0] == "cancel_then_raise":
                    if order_id in self.orders_by_id:
                        self.orders_by_id[order_id].status = ExchangeOrderStatus.CANCELED
                    raise httpx.TimeoutException("timeout after cancel")
                raise event
        snapshot = self.orders_by_id.get(order_id)
        if snapshot is None:
            return False
        snapshot.status = ExchangeOrderStatus.CANCELED
        return True

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        result: list[Order] = []
        for snapshot in self.orders_by_id.values():
            if snapshot.status not in {ExchangeOrderStatus.OPEN, ExchangeOrderStatus.PARTIAL}:
                continue
            if symbol is not None and snapshot.pair_symbol != symbol:
                continue
            result.append(
                Order(
                    order_id=snapshot.order_id,
                    client_order_id=snapshot.client_order_id,
                    symbol=snapshot.pair_symbol,
                    side=snapshot.side,
                    price=float(snapshot.price),
                    quantity=float(snapshot.quantity),
                    status=OrderStatus.OPEN,
                    created_at=datetime.now(UTC) - timedelta(seconds=3600),
                    updated_at=datetime.now(UTC),
                )
            )
        return result

    def _create_open(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None,
    ) -> str:
        self.order_seq += 1
        order_id = str(self.order_seq)
        self.orders_by_id[order_id] = OrderSnapshot(
            order_id=order_id,
            client_order_id=client_order_id,
            pair_symbol=symbol,
            side=side,
            price=Decimal(str(price)),
            quantity=Decimal(str(quantity)),
            status=ExchangeOrderStatus.OPEN,
            timestamp=int(datetime.now(UTC).timestamp() * 1000),
            update_time=int(datetime.now(UTC).timestamp() * 1000),
            status_raw="Open",
        )
        return order_id



def _intent(cycle_id: str = "cycle-a") -> OrderIntent:
    return OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id=cycle_id,
    )



def _service(tmp_path, exchange: ScriptedExchange, **kwargs) -> ExecutionService:
    sleep_fn = kwargs.pop("sleep_fn", (lambda _seconds: None))
    retry_max_delay_ms = kwargs.pop("retry_max_delay_ms", 5)
    return ExecutionService(
        exchange=exchange,
        state_store=StateStore(str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
        submit_retry_max_attempts=2,
        cancel_retry_max_attempts=2,
        retry_base_delay_ms=1,
        retry_max_delay_ms=retry_max_delay_ms,
        sleep_fn=sleep_fn,
        **kwargs,
    )


def test_submit_timeout_sent_response_lost_no_duplicate(tmp_path) -> None:
    exchange = ScriptedExchange()
    exchange.place_plan.append(httpx.TimeoutException("create_then_raise"))
    service = _service(tmp_path, exchange)

    assert service.execute_intents([_intent("cycle-timeout")]) == 1
    assert service.execute_intents([_intent("cycle-timeout")]) == 0
    assert exchange.place_calls == 1
    assert len(exchange.orders_by_id) == 1


def test_submit_429_honors_retry_after(tmp_path, monkeypatch) -> None:
    sleeps: list[float] = []
    exchange = ScriptedExchange()
    exchange.place_plan.append(ExchangeError("status=429", response_body="Retry-After: 2"))
    service = _service(tmp_path, exchange, sleep_fn=sleeps.append, retry_max_delay_ms=5000)

    assert service.execute_intents([_intent("cycle-429")]) == 1
    assert exchange.place_calls == 2
    assert sleeps == [2.0]


def test_submit_connection_reset_before_send_becomes_failed(tmp_path) -> None:
    exchange = ScriptedExchange()
    exchange.place_plan.append(RuntimeError("connection reset by peer"))
    service = _service(tmp_path, exchange)

    assert service.execute_intents([_intent("cycle-rst")]) == 0
    assert len(exchange.orders_by_id) == 0


def test_cancel_timeout_maybe_canceled_reconciles(tmp_path) -> None:
    exchange = ScriptedExchange()
    service = _service(tmp_path, exchange)
    assert service.execute_intents([_intent("cycle-cancel-timeout")]) == 1

    order_id = next(iter(exchange.orders_by_id.keys()))
    exchange.cancel_plan.append(httpx.TimeoutException("cancel_then_raise"))
    assert service.cancel_stale_orders("cycle-cancel-timeout-2") == 1
    with service.state_store._connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    assert row["status"] == "canceled"


def test_cancel_429_retries(tmp_path) -> None:
    exchange = ScriptedExchange()
    service = _service(tmp_path, exchange)
    assert service.execute_intents([_intent("cycle-cancel-429")]) == 1

    exchange.cancel_plan.append(ExchangeError("status=429", response_body="Retry-After=1"))
    assert service.cancel_stale_orders("cycle-cancel-429-2") == 1
    assert exchange.cancel_calls == 2


def test_partial_fill_then_cancel_request(tmp_path) -> None:
    exchange = ScriptedExchange()
    service = _service(tmp_path, exchange)
    assert service.execute_intents([_intent("cycle-partial")]) == 1

    order_id = next(iter(exchange.orders_by_id.keys()))
    exchange.orders_by_id[order_id].status = ExchangeOrderStatus.PARTIAL
    assert service.cancel_stale_orders("cycle-partial-2") == 1
    assert exchange.orders_by_id[order_id].status == ExchangeOrderStatus.CANCELED


def test_restart_recovery_with_unknown_record_converges(tmp_path) -> None:
    exchange = ScriptedExchange()
    service = _service(tmp_path, exchange, unknown_reprobe_initial_seconds=1, unknown_reprobe_escalation_attempts=3)
    assert service.execute_intents([_intent("cycle-restart")]) == 1

    order_id = next(iter(exchange.orders_by_id.keys()))
    service.state_store.update_order_status(order_id=order_id, status=OrderStatus.UNKNOWN, reconciled=True)

    restarted = _service(tmp_path, exchange, unknown_reprobe_initial_seconds=1, unknown_reprobe_escalation_attempts=3)
    assert restarted.execute_intents([_intent("cycle-restart")]) == 0
    restarted.refresh_order_lifecycle(["BTC_TRY"])
    assert restarted.safe_mode is False


def test_eventual_visibility_open_orders_resolves_unknown(tmp_path) -> None:
    exchange = ScriptedExchange()
    exchange.place_plan.append(httpx.TimeoutException("create_then_raise"))
    service = _service(tmp_path, exchange)
    assert service.execute_intents([_intent("cycle-late-visible")]) == 1

    order_id = next(iter(exchange.orders_by_id.keys()))
    service.state_store.update_order_status(order_id=order_id, status=OrderStatus.UNKNOWN, reconciled=True)
    service.refresh_order_lifecycle(["BTC_TRY"])

    with service.state_store._connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    assert row["status"] == "open"


def test_unknown_bounded_exceeded_enters_safe_mode_and_emits_decision(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    exchange = ScriptedExchange()
    service = _service(
        tmp_path,
        exchange,
        unknown_reprobe_initial_seconds=1,
        unknown_reprobe_max_seconds=1,
        unknown_reprobe_escalation_attempts=1,
        unknown_reprobe_force_observe_only=True,
    )
    now = datetime.now(UTC)
    service.state_store.save_order(
        Order(
            order_id="u-1",
            client_order_id="cid-u-1",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=100,
            quantity=0.1,
            status=OrderStatus.UNKNOWN,
            created_at=now,
            updated_at=now,
        )
    )
    with service.state_store._connect() as conn:
        conn.execute("UPDATE orders SET unknown_next_probe_at = 0 WHERE order_id = 'u-1'")

    service.refresh_order_lifecycle(["BTC_TRY"])

    assert service.safe_mode is True
    decision_logs = [r for r in caplog.records if r.msg == "decision_event"]
    assert decision_logs
    payload = decision_logs[-1].extra
    assert payload["decision_layer"] == "execution"
    assert payload["reason_code"] == "execution_reconcile:unknown_bounded_exceeded"
    assert payload["action"] == "SAFE_MODE"


def test_cancel_stale_skips_order_submitted_same_cycle(tmp_path) -> None:
    exchange = ScriptedExchange()
    service = _service(tmp_path, exchange)
    assert service.execute_intents([_intent("cycle-skew")]) == 1

    assert service.cancel_stale_orders("cycle-skew") == 0
    assert exchange.cancel_calls == 0


def test_replay_same_cycle_twice_faults_have_single_side_effect(tmp_path) -> None:
    exchange = ScriptedExchange()
    exchange.place_plan.append(httpx.TimeoutException("create_then_raise"))
    service = _service(tmp_path, exchange)
    intent = _intent("cycle-replay")

    assert service.execute_intents([intent]) == 1
    assert service.execute_intents([intent]) == 0

    with service.state_store._connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM idempotency_keys WHERE action_type='place_order'"
        ).fetchone()
    assert rows["c"] == 1
    assert len(exchange.orders_by_id) == 1
