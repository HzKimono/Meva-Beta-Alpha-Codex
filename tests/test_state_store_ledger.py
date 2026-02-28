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


def test_ledger_incremental_rowid_and_checkpoint_io(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger_checkpoint.db"))
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    events = [
        LedgerEvent(
            event_id="e1",
            ts=ts,
            symbol="BTCTRY",
            type=LedgerEventType.FEE,
            side=None,
            qty=Decimal("0"),
            price=None,
            fee=Decimal("1"),
            fee_currency="TRY",
            exchange_trade_id="fee:e1",
            exchange_order_id=None,
            client_order_id=None,
            meta={},
        ),
        LedgerEvent(
            event_id="e2",
            ts=ts,
            symbol="BTCTRY",
            type=LedgerEventType.FEE,
            side=None,
            qty=Decimal("0"),
            price=None,
            fee=Decimal("2"),
            fee_currency="TRY",
            exchange_trade_id="fee:e2",
            exchange_order_id=None,
            client_order_id=None,
            meta={},
        ),
    ]
    store.append_ledger_events(events)

    assert store.get_latest_ledger_event_rowid() >= 2
    events_after_zero, max_rowid_zero = store.load_ledger_events_after_rowid(last_rowid=0)
    events_after_one, max_rowid_one = store.load_ledger_events_after_rowid(last_rowid=1)
    assert len(events_after_zero) == 2
    assert len(events_after_one) == 1
    assert max_rowid_zero >= 2
    assert max_rowid_one >= 2

    store.upsert_ledger_checkpoint(
        scope_id="stage7",
        last_rowid=2,
        snapshot_json='{"symbols":{},"fees_by_currency":{}}',
        snapshot_version=1,
        updated_at=ts.isoformat(),
    )
    checkpoint = store.get_ledger_checkpoint("stage7")
    assert checkpoint is not None
    assert checkpoint.last_rowid == 2
    assert checkpoint.snapshot_version == 1


def test_checkpoint_equivalence_full_replay_vs_resume(tmp_path) -> None:
    import logging

    from btcbot.domain.ledger import LedgerState, apply_events
    from btcbot.services.ledger_service import LedgerService

    store = StateStore(db_path=str(tmp_path / "checkpoint_equivalence.db"))
    base_ts = datetime(2026, 1, 2, tzinfo=UTC)
    events = [
        LedgerEvent(
            event_id="fill-b1",
            ts=base_ts,
            symbol="BTCTRY",
            type=LedgerEventType.FILL,
            side="BUY",
            qty=Decimal("1"),
            price=Decimal("100"),
            fee=None,
            fee_currency=None,
            exchange_trade_id="fill-b1",
            exchange_order_id=None,
            client_order_id=None,
            meta={},
        ),
        LedgerEvent(
            event_id="fill-s1",
            ts=base_ts,
            symbol="BTCTRY",
            type=LedgerEventType.FILL,
            side="SELL",
            qty=Decimal("0.4"),
            price=Decimal("120"),
            fee=None,
            fee_currency=None,
            exchange_trade_id="fill-s1",
            exchange_order_id=None,
            client_order_id=None,
            meta={},
        ),
        LedgerEvent(
            event_id="fee-1",
            ts=base_ts,
            symbol="BTCTRY",
            type=LedgerEventType.FEE,
            side=None,
            qty=Decimal("0"),
            price=None,
            fee=Decimal("1.2"),
            fee_currency="TRY",
            exchange_trade_id="fee-1",
            exchange_order_id=None,
            client_order_id=None,
            meta={},
        ),
    ]
    store.append_ledger_events(events)

    service = LedgerService(state_store=store, logger=logging.getLogger(__name__))
    checkpoint_state, *_ = service.load_state_incremental()

    full_replay_state = apply_events(LedgerState(), store.load_ledger_events())

    assert checkpoint_state == full_replay_state


def test_ledger_service_ingest_is_idempotent_and_fee_namespaced(tmp_path) -> None:
    import logging

    from btcbot.domain.stage4 import Fill
    from btcbot.services.ledger_service import LedgerService

    store = StateStore(db_path=str(tmp_path / "ledger_ingest.db"))
    service = LedgerService(state_store=store, logger=logging.getLogger(__name__))
    fill = Fill(
        fill_id="trade-1",
        order_id="order-1",
        symbol="BTC_TRY",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        fee=Decimal("1"),
        fee_asset="TRY",
        ts=datetime(2024, 1, 1, tzinfo=UTC),
    )

    first = service.ingest_exchange_updates([fill])
    second = service.ingest_exchange_updates([fill])

    assert first.events_inserted == 2
    assert second.events_inserted == 0
    assert second.events_ignored == 2
    rows = store.load_ledger_events()
    assert {row.exchange_trade_id for row in rows} == {"trade-1", "fee:trade-1"}


def test_append_ledger_events_reports_ignored_count_for_duplicate_batch(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger_batch.db"))
    event = LedgerEvent(
        event_id="e1",
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="BTCTRY",
        type=LedgerEventType.FILL,
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        fee=None,
        fee_currency=None,
        exchange_trade_id="t-batch",
        exchange_order_id="o1",
        client_order_id="c1",
        meta={},
    )

    result = store.append_ledger_events([event, event])

    assert result.attempted == 2
    assert result.inserted == 1
    assert result.ignored == 1
