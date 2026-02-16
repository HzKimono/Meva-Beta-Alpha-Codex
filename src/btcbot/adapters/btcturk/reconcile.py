from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True)
class OpenOrderView:
    order_id: str
    client_order_id: str | None
    symbol: str
    status: str
    quantity: Decimal
    remaining_quantity: Decimal
    updated_at: datetime | None


@dataclass(frozen=True)
class FillEvent:
    fill_id: str
    order_id: str | None
    client_order_id: str | None
    symbol: str
    price: Decimal
    quantity: Decimal
    channel: int
    ts: datetime


@dataclass(frozen=True)
class OrderTerminalUpdate:
    order_id: str | None
    client_order_id: str | None
    symbol: str | None
    status: str
    reason: str
    channel: int
    ts: datetime


@dataclass(frozen=True)
class ReconcileResult:
    open_orders: list[OpenOrderView]
    fills: list[FillEvent]
    terminal_updates: list[OrderTerminalUpdate]
    diagnostics: dict[str, int]


@dataclass
class _OrderExecutionAggregate:
    filled_qty: Decimal = Decimal("0")
    last_fill_ts: datetime | None = None
    last_event_ts: datetime | None = None


@dataclass
class ReconcileState:
    seen_fill_ids: set[str] = field(default_factory=set)
    fills: dict[str, FillEvent] = field(default_factory=dict)
    terminal_updates: dict[str, OrderTerminalUpdate] = field(default_factory=dict)
    aggregates_by_order_key: dict[str, _OrderExecutionAggregate] = field(default_factory=dict)


class Reconciler:
    """Merge REST open orders and WS execution events into a consistent idempotent view.

    REST openOrders is the point-in-time truth for currently open orders.
    WS events (423/441 fills and 452 cancel terminal updates) enrich order lifecycle history.
    """

    def merge(
        self,
        *,
        previous: ReconcileState,
        rest_open_orders: list[OpenOrderView],
        ws_fills: list[FillEvent],
        ws_terminal_updates: list[OrderTerminalUpdate],
    ) -> tuple[ReconcileState, ReconcileResult]:
        state = ReconcileState(
            seen_fill_ids=set(previous.seen_fill_ids),
            fills=dict(previous.fills),
            terminal_updates=dict(previous.terminal_updates),
            aggregates_by_order_key=dict(previous.aggregates_by_order_key),
        )

        ws_events_seen = 0
        ws_fill_new = 0
        ws_terminal_new = 0

        for fill in ws_fills:
            ws_events_seen += 1
            if fill.fill_id in state.seen_fill_ids:
                continue
            state.seen_fill_ids.add(fill.fill_id)
            state.fills[fill.fill_id] = fill
            ws_fill_new += 1

            key = self._order_key(fill.order_id, fill.client_order_id)
            aggregate = state.aggregates_by_order_key.setdefault(key, _OrderExecutionAggregate())
            aggregate.filled_qty += fill.quantity
            aggregate.last_fill_ts = fill.ts
            aggregate.last_event_ts = fill.ts

        for terminal in ws_terminal_updates:
            ws_events_seen += 1
            key = self._order_key(terminal.order_id, terminal.client_order_id)
            prior = state.terminal_updates.get(key)
            if prior is None or prior.ts <= terminal.ts:
                state.terminal_updates[key] = terminal
                ws_terminal_new += 1
            aggregate = state.aggregates_by_order_key.setdefault(key, _OrderExecutionAggregate())
            aggregate.last_event_ts = terminal.ts

        open_by_key = {
            self._order_key(order.order_id, order.client_order_id): order
            for order in rest_open_orders
        }

        # Apply explicit terminal updates (e.g., channel 452 cancellation finalized).
        for key, terminal in state.terminal_updates.items():
            if terminal.status.upper() in {"CANCELED", "FILLED"}:
                open_by_key.pop(key, None)

        result = ReconcileResult(
            open_orders=sorted(open_by_key.values(), key=lambda item: (item.symbol, item.order_id)),
            fills=sorted(state.fills.values(), key=lambda item: (item.ts, item.fill_id)),
            terminal_updates=sorted(
                state.terminal_updates.values(),
                key=lambda item: (item.ts, item.status, item.order_id or ""),
            ),
            diagnostics={
                "ws_events_seen": ws_events_seen,
                "ws_fill_new": ws_fill_new,
                "ws_terminal_new": ws_terminal_new,
                "rest_open_orders_count": len(rest_open_orders),
                "open_orders_after_merge": len(open_by_key),
            },
        )
        return state, result

    def _order_key(self, order_id: str | None, client_order_id: str | None) -> str:
        if order_id:
            return f"oid:{order_id}"
        if client_order_id:
            return f"cid:{client_order_id}"
        return "unknown"


def now_utc() -> datetime:
    return datetime.now(UTC)
