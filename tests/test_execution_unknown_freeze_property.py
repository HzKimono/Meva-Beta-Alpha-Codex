from __future__ import annotations

from datetime import UTC, datetime

import pytest

try:
    from hypothesis import given
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - optional dependency
    pytestmark = pytest.mark.skip(reason="hypothesis is not installed")

    def given(*_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator

    class _MissingStrategies:
        @staticmethod
        def lists(*_args, **_kwargs):
            return None

        @staticmethod
        def sampled_from(*_args, **_kwargs):
            return None

    st = _MissingStrategies()

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import (
    Balance,
    ExchangeError,
    OpenOrders,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    PairInfo,
    SymbolRules,
)
from btcbot.services.execution_service import ExecutionService, SubmitBlockedDueToUnknownError
from btcbot.services.state_store import StateStore


class _FakeMarketDataService:
    def get_symbol_rules(self, pair_symbol: str) -> SymbolRules:
        return SymbolRules(pair_symbol=pair_symbol, price_scale=2, quantity_scale=4)


class _FakeExchange(ExchangeClient):
    def __init__(self) -> None:
        self.place_calls = 0

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

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int):
        del pair_symbol, start_ms, end_ms
        return []

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ) -> Order:
        del symbol, side, price, quantity, client_order_id
        self.place_calls += 1
        now = datetime.now(UTC)
        return Order(
            order_id=f"o-{self.place_calls}",
            client_order_id=None,
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=100.0,
            quantity=0.1,
            status=OrderStatus.NEW,
            created_at=now,
            updated_at=now,
        )

    def cancel_order(self, order_id: str) -> bool:
        del order_id
        return True

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        del symbol
        return []


def _intent(cycle_id: str) -> OrderIntent:
    return OrderIntent(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        notional=10.0,
        cycle_id=cycle_id,
    )


@given(st.lists(st.sampled_from(["unknown_on", "unknown_off", "submit", "reconcile_fail", "reconcile_ok"]), min_size=1, max_size=25))
def test_submit_never_reaches_exchange_when_unknown_present(tmp_path, events) -> None:
    exchange = _FakeExchange()
    service = ExecutionService(
        exchange=exchange,
        state_store=StateStore(str(tmp_path / "state.db")),
        market_data_service=_FakeMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    logical_unknown = False
    for idx, event in enumerate(events):
        if event == "unknown_on":
            now = datetime.now(UTC)
            service.state_store.save_order(
                Order(
                    order_id="unknown:prop",
                    client_order_id="cid-prop",
                    symbol="BTCTRY",
                    side=OrderSide.BUY,
                    price=100,
                    quantity=0.1,
                    status=OrderStatus.UNKNOWN,
                    created_at=now,
                    updated_at=now,
                )
            )
            logical_unknown = True
            continue

        if event == "unknown_off":
            service.state_store.update_order_status(
                order_id="unknown:prop",
                status=OrderStatus.FILLED,
                reconciled=True,
            )
            logical_unknown = False
            continue

        if event == "reconcile_fail":
            exchange.get_open_orders = lambda _pair_symbol: (_ for _ in ()).throw(ExchangeError("status=429"))
            service.refresh_order_lifecycle(["BTC_TRY"])
            continue

        if event == "reconcile_ok":
            exchange.get_open_orders = lambda _pair_symbol: OpenOrders(bids=[], asks=[])
            service.refresh_order_lifecycle(["BTC_TRY"])
            logical_unknown = bool(service.state_store.list_unknown_orders())
            continue

        before = exchange.place_calls
        if logical_unknown:
            with pytest.raises(SubmitBlockedDueToUnknownError):
                service.execute_intents([_intent(f"cycle-{idx}")])
            assert exchange.place_calls == before
        else:
            service.execute_intents([_intent(f"cycle-{idx}")])
            assert exchange.place_calls >= before
