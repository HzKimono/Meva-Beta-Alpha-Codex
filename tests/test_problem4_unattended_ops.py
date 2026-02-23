from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.config import Settings
from btcbot.domain.models import Balance, OpenOrders, Order, OrderIntent, OrderSide, OrderStatus, PairInfo
from btcbot.services.execution_service import ExecutionService
from btcbot.services.stage7_cycle_runner import Stage7CycleRunner
from btcbot.services.state_store import StateStore


class _MiniExchange:
    def __init__(self) -> None:
        self.placed: list[tuple[str, OrderSide, float, float, str | None]] = []

    def get_balances(self) -> list[Balance]:
        return [Balance(asset="TRY", free=Decimal("100000")), Balance(asset="BTC", free=Decimal("1"))]

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        del pair_symbol
        return OpenOrders(bids=[], asks=[])

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int):
        del pair_symbol, start_ms, end_ms
        return []

    def get_order(self, order_id: str):
        raise ValueError(order_id)

    def get_exchange_info(self) -> list[PairInfo]:
        return []

    def place_limit_order(self, symbol: str, side: OrderSide, price: float, quantity: float, client_order_id: str | None = None) -> Order:
        self.placed.append((symbol, side, price, quantity, client_order_id))
        return Order(
            order_id="oid-1",
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            status=OrderStatus.OPEN,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def cancel_order(self, order_id: str) -> bool:
        del order_id
        return True

    def health_snapshot(self) -> dict[str, object]:
        return {"degraded": True, "breaker_open": True, "recommended_sleep_seconds": 5.0}


def test_state_store_idempotency_reservation_lifecycle(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    assert store.reserve_idempotency("k1") is True
    assert store.reserve_idempotency("k1") is False
    store.fail_idempotency("k1", "boom")
    assert store.reserve_idempotency("k1") is True
    store.commit_idempotency("k1", "ex-1")
    assert store.reserve_idempotency("k1") is False


def test_execution_service_dry_run_tracks_would_submit(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _MiniExchange()
    service = ExecutionService(exchange=exchange, state_store=store, dry_run=True, kill_switch=False)
    intent = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="c1",
    )
    placed = service.execute_intents([intent])
    assert placed >= 0
    assert exchange.placed == []
    assert int(service.last_execute_summary["would_submit_orders"]) >= 1


def test_kill_chain_enables_kill_switch_at_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ROLE", "LIVE")
    monkeypatch.setenv("KILL_CHAIN_MAX_CONSECUTIVE_ERRORS", "2")
    store = StateStore(db_path=str(tmp_path / "state.db"))
    runner = Stage7CycleRunner()

    def _boom(*args, **kwargs):
        raise RuntimeError("critical")

    monkeypatch.setattr(runner, "run_one_cycle_with_dependencies", _boom)
    settings = Settings(state_db_path=str(tmp_path / "state.db"))

    with pytest.raises(RuntimeError):
        runner.run_one_cycle(settings=settings, state_store=store, exchange=object(), stage4_result=0)
    with pytest.raises(RuntimeError):
        runner.run_one_cycle(settings=settings, state_store=store, exchange=object(), stage4_result=0)

    enabled, reason, _ = store.get_kill_switch("LIVE")
    assert enabled is True
    assert reason is not None


def test_api_degrade_skips_submission(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _MiniExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=store,
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    intent = OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id="c2",
    )
    placed = service.execute_intents([intent])
    assert placed == 0
    assert exchange.placed == []
