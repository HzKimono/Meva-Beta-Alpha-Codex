from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.ledger import (
    LedgerEvent,
    LedgerEventType,
    LedgerState,
    apply_events,
    compute_realized_pnl,
    compute_unrealized_pnl,
)


def _event(
    event_id: str,
    ts: datetime,
    symbol: str,
    type_: LedgerEventType,
    side: str | None,
    qty: str,
    price: str | None,
    fee: str | None = None,
    fee_currency: str | None = None,
    exchange_trade_id: str | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id,
        ts=ts,
        symbol=symbol,
        type=type_,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price) if price is not None else None,
        fee=Decimal(fee) if fee is not None else None,
        fee_currency=fee_currency,
        exchange_trade_id=exchange_trade_id,
        exchange_order_id=None,
        client_order_id=None,
        meta={},
    )


def test_fifo_realized_unrealized_and_fee_handling() -> None:
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    events = [
        _event("1", ts, "BTCTRY", LedgerEventType.FILL, "BUY", "1", "100", exchange_trade_id="t1"),
        _event("2", ts, "BTCTRY", LedgerEventType.FILL, "BUY", "1", "110", exchange_trade_id="t2"),
        _event(
            "3", ts, "BTCTRY", LedgerEventType.FILL, "SELL", "1.5", "120", exchange_trade_id="t3"
        ),
        _event(
            "4",
            ts,
            "BTCTRY",
            LedgerEventType.FEE,
            None,
            "0",
            None,
            fee="2",
            fee_currency="TRY",
        ),
    ]
    state = apply_events(LedgerState(), events)
    assert compute_realized_pnl(state) == Decimal("25")
    assert state.fees_by_currency["TRY"] == Decimal("2")
    assert compute_unrealized_pnl(state, {"BTCTRY": Decimal("130")}) == Decimal("10")


def test_deterministic_sort_by_ts_event_id() -> None:
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    a = _event("a", ts, "BTCTRY", LedgerEventType.FILL, "BUY", "1", "100", exchange_trade_id="x1")
    b = _event(
        "b", ts, "BTCTRY", LedgerEventType.FILL, "SELL", "0.5", "110", exchange_trade_id="x2"
    )
    state1 = apply_events(LedgerState(), [a, b])
    state2 = apply_events(LedgerState(), [b, a])
    assert compute_realized_pnl(state1) == compute_realized_pnl(state2)
    assert compute_unrealized_pnl(state1, {"BTCTRY": Decimal("120")}) == compute_unrealized_pnl(
        state2, {"BTCTRY": Decimal("120")}
    )


def test_oversell_raises_invariant_violation() -> None:
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    events = [
        _event("1", ts, "BTCTRY", LedgerEventType.FILL, "BUY", "0.5", "100"),
        _event("2", ts, "BTCTRY", LedgerEventType.FILL, "SELL", "1.0", "110"),
    ]

    try:
        apply_events(LedgerState(), events)
    except ValueError as exc:
        message = str(exc)
        assert "oversell_invariant_violation" in message
        assert "BTCTRY" in message
    else:
        raise AssertionError("expected oversell invariant violation")


def test_sorting_normalizes_naive_utc_ts() -> None:
    aware_later = datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)
    naive_earlier = datetime(2024, 1, 1, 0, 0, 0)
    events = [
        _event("a", naive_earlier, "BTCTRY", LedgerEventType.FILL, "BUY", "1", "100"),
        _event("b", aware_later, "BTCTRY", LedgerEventType.FILL, "SELL", "0.5", "110"),
    ]
    state = apply_events(LedgerState(), events)
    assert compute_realized_pnl(state) == Decimal("5")
