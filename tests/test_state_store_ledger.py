from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.services.state_store import StateStore


def test_append_ledger_events_dedupes_exchange_trade_id(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger.db"))
    event1 = LedgerEvent(
        event_id="e1",
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="BTCTRY",
        type=LedgerEventType.FILL,
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        fee=None,
        fee_currency=None,
        exchange_trade_id="t1",
        exchange_order_id="o1",
        client_order_id="c1",
        meta={},
    )
    event2 = LedgerEvent(
        event_id="e2",
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="BTCTRY",
        type=LedgerEventType.FILL,
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        fee=None,
        fee_currency=None,
        exchange_trade_id="t1",
        exchange_order_id="o2",
        client_order_id="c2",
        meta={},
    )

    first = store.append_ledger_events([event1])
    second = store.append_ledger_events([event2])
    rows = store.load_ledger_events()

    assert first.inserted == 1
    assert second.inserted == 0
    assert len(rows) == 1


def test_append_ledger_fee_events_dedupes_exchange_trade_id(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger_fee.db"))
    event1 = LedgerEvent(
        event_id="fee-e1",
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="BTCTRY",
        type=LedgerEventType.FEE,
        side=None,
        qty=Decimal("0"),
        price=None,
        fee=Decimal("1"),
        fee_currency="TRY",
        exchange_trade_id="fee:t1",
        exchange_order_id="o1",
        client_order_id=None,
        meta={"linked_fill_id": "t1"},
    )
    event2 = LedgerEvent(
        event_id="fee-e2",
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="BTCTRY",
        type=LedgerEventType.FEE,
        side=None,
        qty=Decimal("0"),
        price=None,
        fee=Decimal("1"),
        fee_currency="TRY",
        exchange_trade_id="fee:t1",
        exchange_order_id="o1",
        client_order_id=None,
        meta={"linked_fill_id": "t1"},
    )

    first = store.append_ledger_events([event1])
    second = store.append_ledger_events([event2])
    rows = store.load_ledger_events()

    assert first.inserted == 1
    assert second.inserted == 0
    assert len(rows) == 1
