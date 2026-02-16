from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.models import OrderStatus


@dataclass(frozen=True)
class FillEvent:
    order_id: str
    client_order_id: str | None
    symbol: str
    price: Decimal
    quantity: Decimal
    ts: datetime


@dataclass
class ReconcileState:
    open_orders: dict[str, OrderStatus] = field(default_factory=dict)
    fills: list[FillEvent] = field(default_factory=list)
    last_update: datetime | None = None


class Reconciler:
    """Idempotent merge of REST and WS execution data for each planning cycle."""

    def merge(
        self,
        *,
        previous: ReconcileState,
        rest_open_orders: dict[str, OrderStatus],
        ws_fills: list[FillEvent],
    ) -> ReconcileState:
        fills_by_key = {(fill.order_id, fill.ts.isoformat()): fill for fill in previous.fills}
        for fill in ws_fills:
            fills_by_key[(fill.order_id, fill.ts.isoformat())] = fill

        # Filled/canceled orders should leave the open set.
        merged_open = dict(rest_open_orders)
        for fill in fills_by_key.values():
            merged_open.pop(fill.order_id, None)

        return ReconcileState(
            open_orders=merged_open,
            fills=sorted(fills_by_key.values(), key=lambda fill: fill.ts),
            last_update=datetime.now(UTC),
        )
