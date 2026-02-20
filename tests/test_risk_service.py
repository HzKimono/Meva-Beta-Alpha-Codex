from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.intent import Intent
from btcbot.domain.models import OrderSide, OrderStatus
from btcbot.risk.policy import RiskPolicy
from btcbot.risk.exchange_rules import ExchangeRules
from btcbot.services.risk_service import RiskService


class _StaticRules:
    def get_rules(self, symbol: str) -> ExchangeRules:
        del symbol
        return ExchangeRules(
            min_notional=Decimal("1"),
            price_tick=Decimal("0.1"),
            qty_step=Decimal("0.01"),
        )


@dataclass
class _StoredOrder:
    order_id: str
    symbol: str
    client_order_id: str | None
    status: OrderStatus
    exchange_status_raw: str | None


class _Store:
    def __init__(self) -> None:
        self.orders = [
                        _StoredOrder(
                "open-stale", "BTC_TRY", "cid-stale", OrderStatus.OPEN, "missing_from_open_orders"
            ),
            _StoredOrder("unknown-1", "BTC_TRY", "cid-unknown", OrderStatus.UNKNOWN, None),
        ]
        self.updated: list[tuple[str, OrderStatus, str]] = []

    def find_open_or_unknown_orders(self, *args, **kwargs):
        del args, kwargs
        return list(self.orders)

    def update_order_status(self, *, order_id: str, status: OrderStatus, exchange_status_raw: str, reconciled: bool):
        del reconciled
        self.updated.append((order_id, status, exchange_status_raw))

    def get_last_intent_ts_by_symbol_side(self):
        return {}

    def record_intent(self, intent: Intent, now: datetime) -> None:
        del intent, now


def test_risk_service_reconciles_stale_local_orders_and_allows_new_intent() -> None:
    store = _Store()
    policy = RiskPolicy(
        rules_provider=_StaticRules(),
        max_orders_per_cycle=5,
        max_open_orders_per_symbol=1,
        cooldown_seconds=0,
        notional_cap_try_per_cycle=Decimal("1000"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=lambda: datetime.now(UTC),
    )
    service = RiskService(policy, store)  # type: ignore[arg-type]

    intent = Intent.create(
        cycle_id="c1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        qty=Decimal("0.2"),
        limit_price=Decimal("100"),
        reason="test",
    )

    approved = service.filter("c1", [intent])

    assert len(approved) == 1
    assert ("open-stale", OrderStatus.CANCELED, "reconciled_missing_from_exchange_open_orders") in store.updated
    assert ("unknown-1", OrderStatus.CANCELED, "reconciled_missing_from_exchange_open_orders") in store.updated
