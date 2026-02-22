from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.domain.ledger import (
    LedgerEvent,
    LedgerEventType,
    LedgerState,
    apply_events,
    deserialize_ledger_state,
    serialize_ledger_state,
)
from btcbot.services.ledger_service import LedgerService
from btcbot.services.state_store import StateStore


def _fee_event(event_id: str, ts: datetime, fee: Decimal) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id,
        ts=ts,
        symbol="BTCTRY",
        type=LedgerEventType.FEE,
        side=None,
        qty=Decimal("0"),
        price=None,
        fee=fee,
        fee_currency="TRY",
        exchange_trade_id=event_id,
        exchange_order_id=None,
        client_order_id=None,
        meta={},
    )


def _fill_event(event_id: str, ts: datetime, side: str, qty: Decimal, price: Decimal) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id,
        ts=ts,
        symbol="BTCTRY",
        type=LedgerEventType.FILL,
        side=side,
        qty=qty,
        price=price,
        fee=None,
        fee_currency=None,
        exchange_trade_id=event_id,
        exchange_order_id=None,
        client_order_id=None,
        meta={},
    )


def _assert_state_equal(left: LedgerState, right: LedgerState) -> None:
    assert serialize_ledger_state(left) == serialize_ledger_state(right)
    assert deserialize_ledger_state(serialize_ledger_state(left)) == left


def test_full_replay_and_incremental_checkpoint_parity(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger_incremental.db"))
    service = LedgerService(state_store=store, logger=logging.getLogger(__name__))

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        _fill_event("fill-1", ts, "BUY", Decimal("1"), Decimal("100")),
        _fill_event("fill-2", ts + timedelta(seconds=1), "BUY", Decimal("1"), Decimal("120")),
        _fill_event("fill-3", ts + timedelta(seconds=2), "SELL", Decimal("1.5"), Decimal("150")),
        _fee_event("fee-1", ts + timedelta(seconds=3), Decimal("2.25")),
    ]
    store.append_ledger_events(events)

    full_state = apply_events(LedgerState(), store.load_ledger_events())
    incremental_state, last_rowid, used_checkpoint, applied_events = service.load_state_incremental()

    _assert_state_equal(full_state, incremental_state)
    assert last_rowid > 0
    assert used_checkpoint is False
    assert applied_events == len(events)

    second_state, second_rowid, second_used_checkpoint, second_applied = service.load_state_incremental()
    _assert_state_equal(full_state, second_state)
    assert second_rowid == last_rowid
    assert second_used_checkpoint is True
    assert second_applied == 0


def test_incremental_perf_guard_no_new_events(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger_perf.db"))
    service = LedgerService(state_store=store, logger=logging.getLogger(__name__))

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    events = [_fee_event(f"fee-{idx}", ts + timedelta(seconds=idx), Decimal("1")) for idx in range(50000)]
    store.append_ledger_events(events)

    start_full = time.monotonic()
    _, _, _, full_applied = service.load_state_incremental()
    full_duration = time.monotonic() - start_full

    start_incremental = time.monotonic()
    _, _, _, incremental_applied = service.load_state_incremental()
    incremental_duration = time.monotonic() - start_incremental

    assert full_applied == 50000
    assert incremental_applied == 0
    assert incremental_duration < full_duration * 0.2
