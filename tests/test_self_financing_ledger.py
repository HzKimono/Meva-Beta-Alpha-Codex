from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.accounting.ledger import AccountingLedger
from btcbot.accounting.models import AccountingEventType, AccountingLedgerEvent


def _event(
    event_id: str,
    ts: datetime,
    *,
    type: AccountingEventType,
    symbol: str | None = None,
    side: str | None = None,
    qty: str = "0",
    price_try: str | None = None,
    amount_try: str | None = None,
) -> AccountingLedgerEvent:
    return AccountingLedgerEvent(
        event_id=event_id,
        ts=ts,
        type=type,
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        price_try=Decimal(price_try) if price_try is not None else None,
        amount_try=Decimal(amount_try) if amount_try is not None else None,
        fee_currency="TRY",
        reference_id=None,
        metadata={},
    )


def test_golden_fill_sequence_realized_and_unrealized_pnl() -> None:
    ledger = AccountingLedger()
    base = datetime(2024, 1, 1, tzinfo=UTC)
    events = [
        _event(
            "fill-1",
            base,
            type=AccountingEventType.FILL_RECORDED,
            symbol="BTCTRY",
            side="BUY",
            qty="1",
            price_try="100",
        ),
        _event(
            "fee-1",
            base,
            type=AccountingEventType.FEE_RECORDED,
            symbol="BTCTRY",
            amount_try="0.1",
        ),
        _event(
            "fill-2",
            datetime(2024, 1, 2, tzinfo=UTC),
            type=AccountingEventType.FILL_RECORDED,
            symbol="BTCTRY",
            side="SELL",
            qty="0.4",
            price_try="130",
        ),
        _event(
            "fee-2",
            datetime(2024, 1, 2, tzinfo=UTC),
            type=AccountingEventType.FEE_RECORDED,
            symbol="BTCTRY",
            amount_try="0.05",
        ),
    ]

    state = ledger.recompute(
        events=events,
        as_of=datetime(2024, 1, 2, tzinfo=UTC),
        mark_prices_try={"BTCTRY": Decimal("125")},
        initial_trading_capital_try=Decimal("1000"),
    )

    assert state.realized_pnl_try == Decimal("12.00000000")
    assert state.unrealized_pnl_try == Decimal("15.00000000")
    assert state.fees_try == Decimal("0.15000000")


def test_idempotent_duplicate_fill_event_ids_do_not_double_count() -> None:
    ledger = AccountingLedger()
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    events = [
        _event(
            "fill-dup",
            ts,
            type=AccountingEventType.FILL_RECORDED,
            symbol="ETHTRY",
            side="BUY",
            qty="2",
            price_try="50",
        ),
        _event(
            "fill-dup",
            ts,
            type=AccountingEventType.FILL_RECORDED,
            symbol="ETHTRY",
            side="BUY",
            qty="2",
            price_try="50",
        ),
    ]
    state = ledger.recompute(
        events=events,
        as_of=ts,
        mark_prices_try={"ETHTRY": Decimal("50")},
        initial_trading_capital_try=Decimal("500"),
    )

    assert state.symbols["ETHTRY"].qty == Decimal("2.00000000")
    assert state.balances_try["TRY"] == Decimal("400.00000000")


def test_stress_partial_fills_fee_funding_and_rounding() -> None:
    ledger = AccountingLedger()
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    events = [
        _event(
            "f1",
            ts,
            type=AccountingEventType.FILL_RECORDED,
            symbol="XRPTRY",
            side="BUY",
            qty="3.33333333",
            price_try="10.001",
        ),
        _event(
            "fee1",
            ts,
            type=AccountingEventType.FEE_RECORDED,
            symbol="XRPTRY",
            amount_try="0.333333339",
        ),
        _event(
            "fund1",
            ts,
            type=AccountingEventType.FUNDING_COST_RECORDED,
            symbol="XRPTRY",
            amount_try="0.100000004",
        ),
        _event(
            "slip1",
            ts,
            type=AccountingEventType.SLIPPAGE_RECORDED,
            symbol="XRPTRY",
            amount_try="0.050000009",
        ),
        _event(
            "f2",
            datetime(2024, 1, 2, tzinfo=UTC),
            type=AccountingEventType.FILL_RECORDED,
            symbol="XRPTRY",
            side="SELL",
            qty="1.11111111",
            price_try="10.251",
        ),
    ]

    state = ledger.recompute(
        events=events,
        as_of=datetime(2024, 1, 2, tzinfo=UTC),
        mark_prices_try={"XRPTRY": Decimal("10.40")},
        initial_trading_capital_try=Decimal("1000"),
    )

    assert state.fees_try == Decimal("0.33333334")
    assert state.funding_cost_try == Decimal("0.10000000")
    assert state.slippage_try == Decimal("0.05000001")
    assert state.symbols["XRPTRY"].qty == Decimal("2.22222222")
