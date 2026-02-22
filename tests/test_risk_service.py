from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.intent import Intent
from btcbot.domain.models import OrderSide, OrderStatus
from btcbot.services.risk_service import RiskService


@dataclass
class _StoredOrder:
    order_id: str
    symbol: str
    client_order_id: str | None
    status: OrderStatus
    exchange_status_raw: str | None
    unknown_probe_attempts: int = 0
    unknown_first_seen_at: int | None = None


class _CapturePolicy:
    def __init__(self) -> None:
        self.context = None

    def evaluate(self, context, intents):
        self.context = context
        return list(intents)


class _Store:
    def __init__(self, orders: list[_StoredOrder]) -> None:
        self.orders = list(orders)
        self.updated: list[tuple[str, OrderStatus, str]] = []

    def find_open_or_unknown_orders(self, *args, **kwargs):
        del args, kwargs
        return list(self.orders)

    def update_order_status(
        self,
        *,
        order_id: str,
        status: OrderStatus,
        exchange_status_raw: str,
        reconciled: bool,
    ):
        del reconciled
        self.updated.append((order_id, status, exchange_status_raw))

    def get_last_intent_ts_by_symbol_side(self):
        return {}

    def record_intent(self, intent: Intent, now: datetime) -> None:
        del intent, now


def _intent() -> Intent:
    return Intent.create(
        cycle_id="c1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        qty=Decimal("0.2"),
        limit_price=Decimal("100"),
        reason="test",
    )


def test_risk_service_phantom_unknown_is_closed_after_threshold_and_not_counted() -> None:
    store = _Store(
        [
            _StoredOrder(
                "unknown-1",
                "BTC_TRY",
                "cid-unknown",
                OrderStatus.UNKNOWN,
                "missing_from_open_orders",
                unknown_probe_attempts=2,
            )
        ]
    )
    policy = _CapturePolicy()
    service = RiskService(policy, store)  # type: ignore[arg-type]

    approved = service.filter("c1", [_intent()])

    assert len(approved) == 1
    assert (
        "unknown-1",
        OrderStatus.CANCELED,
        "reconciled_missing_from_exchange_open_orders",
    ) in store.updated
    assert policy.context.open_orders_by_symbol.get("BTCTRY", 0) == 0


def test_risk_service_late_fill_safety_does_not_close_on_first_missing_observation() -> None:
    store = _Store(
        [
            _StoredOrder(
                "unknown-1",
                "BTC_TRY",
                "cid-unknown",
                OrderStatus.UNKNOWN,
                "missing_from_open_orders",
                unknown_probe_attempts=1,
            )
        ]
    )
    policy = _CapturePolicy()
    service = RiskService(policy, store)  # type: ignore[arg-type]

    approved = service.filter("c1", [_intent()])

    assert len(approved) == 1
    assert store.updated == []
    assert policy.context.open_orders_by_symbol.get("BTCTRY", 0) == 0


def test_risk_service_counts_reconciled_open_orders_and_identifiers() -> None:
    store = _Store([_StoredOrder("open-1", "BTC_TRY", "cid-open", OrderStatus.OPEN, "open")])
    policy = _CapturePolicy()
    service = RiskService(policy, store)  # type: ignore[arg-type]

    approved = service.filter("c1", [_intent()])

    assert len(approved) == 1
    assert policy.context.open_orders_by_symbol["BTCTRY"] == 1
    assert policy.context.open_order_identifiers_by_symbol["BTCTRY"] == ["cid-open"]
    assert policy.context.open_order_count_origin_by_symbol["BTCTRY"] == "reconciled"


def test_risk_service_phantom_unknown_is_closed_after_time_window() -> None:
    first_seen_ms = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    store = _Store(
        [
            _StoredOrder(
                "unknown-2",
                "BTC_TRY",
                "cid-unknown-2",
                OrderStatus.UNKNOWN,
                "missing_from_open_orders",
                unknown_probe_attempts=0,
                unknown_first_seen_at=first_seen_ms,
            )
        ]
    )
    policy = _CapturePolicy()
    service = RiskService(
        policy,
        store,  # type: ignore[arg-type]
        now_provider=lambda: datetime(2025, 1, 1, 0, 10, tzinfo=UTC),
    )

    approved = service.filter("c1", [_intent()])

    assert len(approved) == 1
    assert (
        "unknown-2",
        OrderStatus.CANCELED,
        "reconciled_missing_from_exchange_open_orders",
    ) in store.updated
