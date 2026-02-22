from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.ledger import (
    LedgerEvent,
    LedgerEventType,
    LedgerState,
    apply_events,
    compute_realized_pnl,
)
from btcbot.domain.money_policy import (
    MoneyMathPolicy,
    round_fee,
    round_price,
    round_qty,
    round_quote,
    to_decimal,
)


def _event(
    event_id: str,
    ts: datetime,
    side: str | None,
    qty: str,
    price: str | None,
    fee: str | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id,
        ts=ts,
        symbol="BTCTRY",
        type=LedgerEventType.FILL if side else LedgerEventType.FEE,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price) if price is not None else None,
        fee=Decimal(fee) if fee is not None else None,
        fee_currency="TRY" if fee is not None else None,
        exchange_trade_id=event_id,
        exchange_order_id=None,
        client_order_id=None,
        meta={},
    )


def test_roundtrip_price_tick_and_qty_step() -> None:
    policy = MoneyMathPolicy(price_tick=Decimal("0.01"), qty_step=Decimal("0.0001"))

    assert round_price(Decimal("100.009"), policy) == Decimal("100.00")
    assert round_price(Decimal("100.010"), policy) == Decimal("100.01")
    assert round_qty(Decimal("0.12349"), policy) == Decimal("0.1234")
    assert round_qty(Decimal("0.12340"), policy) == Decimal("0.1234")


def test_fee_rounding_consistency() -> None:
    policy = MoneyMathPolicy(
        price_tick=Decimal("0.01"), qty_step=Decimal("0.0001"), fee_precision=8
    )

    price = round_price(Decimal("100.019"), policy)
    qty = round_qty(Decimal("0.123456"), policy)
    fee_rate = Decimal("0.001")

    fee_path_a = round_fee(price * qty * fee_rate, policy)
    fee_path_b = round_fee(
        round_price(Decimal("100.019"), policy) * round_qty(Decimal("0.123456"), policy) * fee_rate,
        policy,
    )
    assert fee_path_a == fee_path_b


def test_pnl_drift_epsilon() -> None:
    policy = MoneyMathPolicy(
        price_tick=Decimal("0.01"), qty_step=Decimal("0.00000001"), epsilon=Decimal("0.00000001")
    )
    ts = datetime(2024, 1, 1, tzinfo=UTC)

    events = [
        _event("b1", ts, "BUY", "1.23456", "100.019"),
        _event("f1", ts, None, "0", None, fee="0.123456789"),
        _event("b2", ts, "BUY", "0.76543", "101.017"),
        _event("f2", ts, None, "0", None, fee="0.076543219"),
        _event("s1", ts, "SELL", "1.50000", "105.011"),
        _event("f3", ts, None, "0", None, fee="0.157516500"),
    ]

    def policy_resolver(symbol: str):
        return policy

    state = apply_events(LedgerState(), events, policy_resolver=policy_resolver)
    ledger_realized = compute_realized_pnl(state, policy_resolver=policy_resolver)
    ledger_fees = state.fees_by_currency.get("TRY", Decimal("0"))
    ledger_net = round_quote(ledger_realized - ledger_fees, policy)

    buy_1_qty = round_qty(Decimal("1.23456"), policy)
    buy_1_price = round_price(Decimal("100.019"), policy)
    buy_2_qty = round_qty(Decimal("0.76543"), policy)
    buy_2_price = round_price(Decimal("101.017"), policy)
    sell_qty = round_qty(Decimal("1.50000"), policy)
    sell_price = round_price(Decimal("105.011"), policy)

    matched_1 = min(sell_qty, buy_1_qty)
    rem = round_qty(sell_qty - matched_1, policy)
    matched_2 = min(rem, buy_2_qty)

    direct_realized = round_quote(
        (sell_price - buy_1_price) * matched_1 + (sell_price - buy_2_price) * matched_2,
        policy,
    )
    direct_fees = round_fee(
        Decimal("0.123456789") + Decimal("0.076543219") + Decimal("0.157516500"),
        policy,
    )
    direct_net = round_quote(direct_realized - direct_fees, policy)

    assert abs(ledger_net - direct_net) <= policy.epsilon


def test_no_float_leakage() -> None:
    policy = MoneyMathPolicy(price_tick=Decimal("0.01"), qty_step=Decimal("0.0001"))
    assert isinstance(round_price(Decimal("1.23"), policy), Decimal)
    assert isinstance(round_qty(Decimal("1.23"), policy), Decimal)
    assert isinstance(round_fee(Decimal("1.23"), policy), Decimal)
    assert isinstance(round_quote(Decimal("1.23"), policy), Decimal)

    try:
        to_decimal(1.23)
    except TypeError:
        pass
    else:
        raise AssertionError("to_decimal must reject float input")
