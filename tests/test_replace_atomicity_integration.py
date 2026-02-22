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
        self.cancels: list[str] = []

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

    def submit_limit_order(self, symbol: str, side: str, price: Decimal, qty: Decimal, client_order_id: str) -> OrderAck:
        del symbol, side, price, qty
        self.submits.append(client_order_id)
        return OrderAck(exchange_order_id=f"ex-{client_order_id}", status="submitted")

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool:
        self.cancels.append(exchange_order_id)
        return True


@dataclass
class FakeReplaceTxn:
    status: str


class FakeStateStore:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.orders: dict[str, Order] = {
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
        self.replace: dict[str, FakeReplaceTxn] = {}

    def stage4_has_unknown_orders(self) -> bool:
        return False

    def stage4_unknown_client_order_ids(self) -> list[str]:
        return []

    def stage4_submit_dedupe_status(self, *, internal_client_order_id: str, exchange_client_order_id: str):
        del internal_client_order_id, exchange_client_order_id

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

    def record_stage4_order_submitted(self, *, symbol: str, client_order_id: str, exchange_client_id: str, exchange_order_id: str, side: str, price: Decimal, qty: Decimal, mode: str, status: str) -> None:
        now = datetime.now(UTC)
        self.orders[client_order_id] = Order(
            symbol=symbol,
            side=side,
            type="limit",
            price=price,
            qty=qty,
            status=status,
            created_at=now,
            updated_at=now,
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
            exchange_client_id=exchange_client_id,
            mode=mode,
        )

    def is_order_terminal(self, client_order_id: str) -> bool:
        return self.orders[client_order_id].status in {"canceled", "filled", "rejected", "unknown_closed"}

    def get_stage4_order_by_client_id(self, client_order_id: str):
        return self.orders.get(client_order_id)

    def record_stage4_order_cancel_requested(self, client_order_id: str) -> None:
        order = self.orders[client_order_id]
        self.orders[client_order_id] = Order(**{**order.__dict__, "status": "cancel_requested"})

    def record_stage4_order_canceled(self, client_order_id: str) -> None:
        order = self.orders[client_order_id]
        self.orders[client_order_id] = Order(**{**order.__dict__, "status": "canceled"})

    def upsert_stage4_replace_transaction(self, *, new_client_order_id: str, old_client_order_id: str, symbol: str, side: str, status: str, last_error: str | None = None) -> None:
        del old_client_order_id, symbol, side, last_error
        self.replace[new_client_order_id] = FakeReplaceTxn(status=status)


def test_replace_flow_integration_with_fakes() -> None:
    exchange = FakeExchange()
    state_store = FakeStateStore()
    svc = ExecutionService(
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

    assert report.canceled == 1
    assert report.submitted == 1
    assert state_store.orders["old"].status == "canceled"
    assert state_store.replace["new"].status == "submitted"
    assert len(exchange.submits) == 1
