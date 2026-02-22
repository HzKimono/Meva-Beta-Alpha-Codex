from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.exchange_stage4 import OrderAck
from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, Order
from btcbot.services.execution_service_stage4 import ExecutionService
from btcbot.services.exchange_rules_service import ExchangeRulesService


class FakeExchange:
    def __init__(self) -> None:
        self.submits: list[str] = []
        self.open_orders: list[Order] = []
        self.uncertain_cancel = False

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
        del symbol
        return list(self.open_orders)

    def submit_limit_order(self, symbol: str, side: str, price: Decimal, qty: Decimal, client_order_id: str) -> OrderAck:
        del symbol, side, price, qty
        self.submits.append(client_order_id)
        return OrderAck(exchange_order_id=f"ex-{client_order_id}", status="submitted")

    def cancel_order_by_exchange_id(self, exchange_order_id: str):
        del exchange_order_id
        if self.uncertain_cancel:
            raise TimeoutError("cancel-timeout")
        return True


@dataclass
class Txn:
    state: str
    last_error: str | None = None


class FakeStateStore:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.orders = {
            "old": Order(
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
        }
        self.replace: dict[str, Txn] = {}

    def stage4_has_unknown_orders(self) -> bool:
        return False

    def stage4_unknown_client_order_ids(self) -> list[str]:
        return []

    def stage4_submit_dedupe_status(self, **kwargs):
        del kwargs

        class Result:
            should_dedupe = False
            dedupe_key = ""
            reason = None
            age_seconds = None
            related_order_id = None
            related_status = None

        return Result()

    def record_stage4_order_rejected(self, *args, **kwargs):
        del args, kwargs

    def record_stage4_order_simulated_submit(self, *args, **kwargs):
        del args, kwargs

    def record_stage4_order_error(self, *args, **kwargs):
        del args, kwargs

    def record_stage4_order_submitted(self, **kwargs) -> None:
        cid = kwargs["client_order_id"]
        now = datetime.now(UTC)
        self.orders[cid] = Order(
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            type="limit",
            price=kwargs["price"],
            qty=kwargs["qty"],
            status=kwargs["status"],
            created_at=now,
            updated_at=now,
            exchange_order_id=kwargs["exchange_order_id"],
            client_order_id=cid,
            exchange_client_id=kwargs["exchange_client_id"],
            mode=kwargs["mode"],
        )

    def is_order_terminal(self, client_order_id: str) -> bool:
        return self.orders[client_order_id].status in {"canceled", "filled", "rejected", "unknown_closed"}

    def get_stage4_order_by_client_id(self, client_order_id: str):
        return self.orders.get(client_order_id)

    def record_stage4_order_cancel_requested(self, client_order_id: str) -> None:
        old = self.orders[client_order_id]
        self.orders[client_order_id] = Order(**{**old.__dict__, "status": "cancel_requested"})

    def record_stage4_order_canceled(self, client_order_id: str) -> None:
        old = self.orders[client_order_id]
        self.orders[client_order_id] = Order(**{**old.__dict__, "status": "canceled"})

    def get_replace_tx(self, replace_tx_id: str):
        return self.replace.get(replace_tx_id)

    def upsert_replace_tx(self, *, replace_tx_id: str, symbol: str, side: str, old_client_order_ids: list[str], new_client_order_id: str, state: str, last_error: str | None = None) -> None:
        del symbol, side, old_client_order_ids, new_client_order_id
        current = self.replace.get(replace_tx_id)
        if current is None:
            self.replace[replace_tx_id] = Txn(state=state, last_error=last_error)

    def update_replace_tx_state(self, *, replace_tx_id: str, state: str, last_error: str | None = None) -> None:
        self.replace[replace_tx_id] = Txn(state=state, last_error=last_error)


def _service(exchange: FakeExchange, state_store: FakeStateStore) -> ExecutionService:
    return ExecutionService(
        exchange=exchange,
        state_store=state_store,  # type: ignore[arg-type]
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


def _replace_actions() -> list[LifecycleAction]:
    return [
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


def test_replace_defers_until_open_order_disappears() -> None:
    exchange = FakeExchange()
    state_store = FakeStateStore()
    exchange.open_orders = [state_store.orders["old"]]
    svc = _service(exchange, state_store)

    first = svc.execute_with_report(_replace_actions())
    assert first.submitted == 0

    exchange.open_orders = []
    second = svc.execute_with_report(_replace_actions())
    assert second.submitted == 1


def test_uncertain_cancel_defers_then_reconcile_allows_submit() -> None:
    exchange = FakeExchange()
    state_store = FakeStateStore()
    exchange.uncertain_cancel = True
    exchange.open_orders = [state_store.orders["old"]]
    svc = _service(exchange, state_store)

    first = svc.execute_with_report(_replace_actions())
    assert first.submitted == 0

    exchange.uncertain_cancel = False
    exchange.open_orders = []
    second = svc.execute_with_report(_replace_actions())
    assert second.submitted == 1
