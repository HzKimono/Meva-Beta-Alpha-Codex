from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.btcturk.reconcile import (
    FillEvent,
    OpenOrderView,
    OrderTerminalUpdate,
    Reconciler,
    ReconcileState,
)


def _ts() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)


def test_partial_fill_does_not_remove_open_order() -> None:
    reconciler = Reconciler()
    open_orders = [
        OpenOrderView(
            order_id="o-1",
            client_order_id="c-1",
            symbol="BTCTRY",
            status="OPEN",
            quantity=Decimal("1"),
            remaining_quantity=Decimal("0.6"),
            updated_at=_ts(),
        )
    ]
    fills = [
        FillEvent(
            fill_id="f-1",
            order_id="o-1",
            client_order_id="c-1",
            symbol="BTCTRY",
            price=Decimal("100"),
            quantity=Decimal("0.4"),
            channel=423,
            ts=_ts(),
        )
    ]
    state, result = reconciler.merge(
        previous=ReconcileState(),
        rest_open_orders=open_orders,
        ws_fills=fills,
        ws_terminal_updates=[],
    )
    assert len(result.open_orders) == 1
    assert state.aggregates_by_order_key["oid:o-1"].filled_qty == Decimal("0.4")


def test_cancel_terminal_update_removes_open_order() -> None:
    reconciler = Reconciler()
    open_orders = [
        OpenOrderView(
            order_id="o-2",
            client_order_id="c-2",
            symbol="ETHTRY",
            status="OPEN",
            quantity=Decimal("2"),
            remaining_quantity=Decimal("2"),
            updated_at=_ts(),
        )
    ]
    terminal = [
        OrderTerminalUpdate(
            order_id="o-2",
            client_order_id="c-2",
            symbol="ETHTRY",
            status="CANCELED",
            reason="channel452",
            channel=452,
            ts=_ts(),
        )
    ]
    _, result = reconciler.merge(
        previous=ReconcileState(),
        rest_open_orders=open_orders,
        ws_fills=[],
        ws_terminal_updates=terminal,
    )
    assert result.open_orders == []


def test_order_matching_prefers_order_id_then_client_order_id() -> None:
    reconciler = Reconciler()
    state, result = reconciler.merge(
        previous=ReconcileState(),
        rest_open_orders=[],
        ws_fills=[
            FillEvent(
                fill_id="f-2",
                order_id=None,
                client_order_id="cid-3",
                symbol="SOLTRY",
                price=Decimal("10"),
                quantity=Decimal("1"),
                channel=441,
                ts=_ts(),
            )
        ],
        ws_terminal_updates=[],
    )
    assert len(result.fills) == 1
    assert "cid:cid-3" in state.aggregates_by_order_key


def test_idempotent_duplicate_ws_fills_not_duplicated() -> None:
    reconciler = Reconciler()
    fill = FillEvent(
        fill_id="f-dup",
        order_id="o-dup",
        client_order_id="c-dup",
        symbol="BTCTRY",
        price=Decimal("100"),
        quantity=Decimal("0.1"),
        channel=423,
        ts=_ts(),
    )
    state1, _ = reconciler.merge(
        previous=ReconcileState(),
        rest_open_orders=[],
        ws_fills=[fill],
        ws_terminal_updates=[],
    )
    _, result2 = reconciler.merge(
        previous=state1,
        rest_open_orders=[],
        ws_fills=[fill],
        ws_terminal_updates=[],
    )
    assert len(result2.fills) == 1
