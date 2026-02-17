from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btcbot.adapters.exchange import ExchangeClient
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
    def __init__(self) -> None:
        self.placed: list[tuple[str, OrderSide, float, float, str | None]] = []
        self.canceled: list[str] = []

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
        self.placed.append((symbol, side, price, quantity, client_order_id))
        return Order(
            order_id="123",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
        )

    def cancel_order(self, order_id: str) -> bool:
        self.canceled.append(order_id)
        return True

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        del symbol
        return []


class FakeMarketDataService:
    def __init__(self, min_total: str = "10") -> None:
        self.min_total = min_total

    def get_symbol_rules(self, pair_symbol: str) -> SymbolRules:
        assert pair_symbol == "BTCTRY"
        return SymbolRules(
            pair_symbol="BTCTRY",
            price_scale=2,
            quantity_scale=4,
            min_total=self.min_total,
        )


def _intent(price: float = 100.009, quantity: float = 0.10009) -> OrderIntent:
    return OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=price,
        quantity=quantity,
        notional=price * quantity,
        cycle_id="cycle-1",
    )


@pytest.mark.parametrize(
    ("dry_run", "kill_switch", "live_enabled"),
    [
        (False, False, False),
        (False, True, True),
    ],
)
def test_live_arming_blocks_side_effects(
    tmp_path, dry_run: bool, kill_switch: bool, live_enabled: bool
) -> None:
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(db_path=str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=dry_run,
        kill_switch=kill_switch,
        live_trading_enabled=live_enabled,
    )

    with pytest.raises(LiveTradingNotArmedError):
        service._ensure_live_side_effects_allowed()


def test_execute_intents_quantizes_before_submit(tmp_path) -> None:
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(db_path=str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent()])

    assert placed == 1
    assert exchange.placed == [("BTCTRY", OrderSide.BUY, 100.0, 0.1, exchange.placed[0][4])]
    assert exchange.placed[0][4] is not None


def test_execute_intents_enforces_min_total(tmp_path) -> None:
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(db_path=str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(min_total="1000"),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent(price=10, quantity=0.1)])

    assert placed == 0
    assert exchange.placed == []


def test_execute_intents_live_not_armed_records_no_place_action(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=False,
    )

    with pytest.raises(LiveTradingNotArmedError):
        service.execute_intents([_intent()])

    assert store.action_count("place_order", service._place_hash(_intent())) == 0


def test_cancel_stale_live_not_armed_records_no_cancel_action(tmp_path) -> None:
    stale_order = Order(
        order_id="o-blocked",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
        updated_at=datetime.now(UTC),
    )

    class OpenExchange(RecordingExchange):
        def list_open_orders(self, symbol: str | None = None) -> list[Order]:
            del symbol
            return [stale_order]

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = OpenExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=False,
    )

    with pytest.raises(LiveTradingNotArmedError):
        service.cancel_stale_orders(cycle_id="cycle-blocked")

    assert store.action_count("cancel_order", service._cancel_hash("o-blocked")) == 0


def test_execute_intents_dry_run_records_would_place_action(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=True,
        kill_switch=False,
        live_trading_enabled=False,
    )

    placed = service.execute_intents([_intent()])

    payload_hash = service._place_hash(_intent())
    assert placed == 1
    assert exchange.placed == []
    assert store.action_count("would_place_order", payload_hash) == 1
    assert store.action_count("place_order", payload_hash) == 0


def test_cancel_stale_dry_run_records_would_cancel_action(tmp_path) -> None:
    stale_order = Order(
        order_id="o-dry",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
        updated_at=datetime.now(UTC),
    )

    class OpenExchange(RecordingExchange):
        def list_open_orders(self, symbol: str | None = None) -> list[Order]:
            del symbol
            return [stale_order]

    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = OpenExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        market_data_service=FakeMarketDataService(),
        dry_run=True,
        kill_switch=False,
        live_trading_enabled=False,
    )

    canceled = service.cancel_stale_orders(cycle_id="cycle-dry")

    payload_hash = service._cancel_hash("o-dry")
    assert canceled == 1
    assert exchange.canceled == []
    assert store.action_count("would_cancel_order", payload_hash) == 1
    assert store.action_count("cancel_order", payload_hash) == 0


def test_execute_intents_blocks_when_ack_missing_even_if_live_enabled(tmp_path) -> None:
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(db_path=str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=False,
    )

    with pytest.raises(LiveTradingNotArmedError) as exc_info:
        service.execute_intents([_intent()])

    assert "ACK_MISSING" in exc_info.value.reasons
    assert exchange.placed == []


def test_execute_intents_allows_when_live_enabled_and_ack_present(tmp_path) -> None:
    exchange = RecordingExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(db_path=str(tmp_path / "state.db")),
        market_data_service=FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    placed = service.execute_intents([_intent()])

    assert placed == 1
    assert len(exchange.placed) == 1
