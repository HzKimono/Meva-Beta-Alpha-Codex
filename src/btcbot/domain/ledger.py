from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from btcbot.domain.money_policy import (
    DEFAULT_MONEY_POLICY,
    MoneyMathPolicy,
    round_fee,
    round_price,
    round_qty,
    round_quote,
)


class LedgerEventType(StrEnum):
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_CANCELED = "ORDER_CANCELED"
    FILL = "FILL"
    FEE = "FEE"
    ADJUSTMENT = "ADJUSTMENT"


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    ts: datetime
    symbol: str
    type: LedgerEventType
    side: str | None
    qty: Decimal
    price: Decimal | None
    fee: Decimal | None
    fee_currency: str | None
    exchange_trade_id: str | None
    exchange_order_id: str | None
    client_order_id: str | None
    meta: dict[str, Any]


@dataclass(frozen=True)
class PositionLot:
    symbol: str
    qty: Decimal
    unit_cost: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class SymbolLedger:
    symbol: str
    lots: tuple[PositionLot, ...] = ()
    realized_pnl: Decimal = Decimal("0")


@dataclass(frozen=True)
class LedgerState:
    symbols: dict[str, SymbolLedger] = field(default_factory=dict)
    fees_by_currency: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerSnapshot:
    cash_try: Decimal
    position_mtm_try: Decimal
    gross_pnl_try: Decimal
    realized_pnl_try: Decimal
    unrealized_pnl_try: Decimal
    net_pnl_try: Decimal
    fees_try: Decimal
    slippage_try: Decimal
    turnover_try: Decimal
    equity_try: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True)
class EquityPoint:
    ts: datetime
    equity_try: Decimal


def _sort_events(events: list[LedgerEvent]) -> list[LedgerEvent]:
    return sorted(events, key=lambda event: (ensure_utc(event.ts), event.event_id))


def apply_events(
    state: LedgerState,
    events: list[LedgerEvent],
    policy_resolver: Callable[[str], MoneyMathPolicy] | None = None,
) -> LedgerState:
    symbol_state = dict(state.symbols)
    fees = dict(state.fees_by_currency)

    for event in _sort_events(events):
        policy = (
            policy_resolver(event.symbol) if policy_resolver is not None else DEFAULT_MONEY_POLICY
        )
        current = symbol_state.get(event.symbol, SymbolLedger(symbol=event.symbol))
        lots = list(current.lots)
        realized = current.realized_pnl

        if (
            event.type == LedgerEventType.FILL
            and event.price is not None
            and event.side is not None
        ):
            qty = round_qty(event.qty, policy)
            price = round_price(event.price, policy)
            if event.side.upper() == "BUY":
                lots.append(
                    PositionLot(
                        symbol=event.symbol,
                        qty=qty,
                        unit_cost=price,
                        opened_at=event.ts,
                    )
                )
            elif event.side.upper() == "SELL":
                remaining = qty
                while remaining > 0 and lots:
                    lot = lots[0]
                    matched = min(remaining, lot.qty)
                    realized = round_quote(realized + ((price - lot.unit_cost) * matched), policy)
                    remaining = round_qty(remaining - matched, policy)
                    leftover = round_qty(lot.qty - matched, policy)
                    if leftover <= 0:
                        lots.pop(0)
                    else:
                        lots[0] = PositionLot(
                            symbol=lot.symbol,
                            qty=leftover,
                            unit_cost=lot.unit_cost,
                            opened_at=lot.opened_at,
                        )
                if remaining > 0:
                    raise ValueError(
                        f"oversell_invariant_violation symbol={event.symbol} "
                        f"remaining_qty={remaining} event_id={event.event_id}"
                    )
        # FEE event invariants: fees are not fills.
        # side=None, qty=0, and price=None are required semantics.
        if event.type == LedgerEventType.FEE and event.fee is not None and event.fee_currency:
            if event.side is not None or event.qty != Decimal("0") or event.price is not None:
                raise ValueError(
                    "fee_event_invariant_violation "
                    f"event_id={event.event_id} side={event.side} "
                    f"qty={event.qty} price={event.price}"
                )
            currency = event.fee_currency.upper()
            if currency == "TRY":
                fees[currency] = round_fee(
                    fees.get(currency, Decimal("0")) + event.fee,
                    policy,
                )
            else:
                fees[currency] = fees.get(currency, Decimal("0")) + event.fee

        if event.type == LedgerEventType.ADJUSTMENT and event.fee is not None:
            realized += event.fee

        symbol_state[event.symbol] = SymbolLedger(
            symbol=event.symbol,
            lots=tuple(lots),
            realized_pnl=realized,
        )

    return LedgerState(symbols=symbol_state, fees_by_currency=fees)


def _serialize_decimal(value: Decimal) -> str:
    return format(value, "f")


def serialize_ledger_state(state: LedgerState) -> str:
    symbols_payload: dict[str, dict[str, object]] = {}
    for symbol in sorted(state.symbols):
        symbol_ledger = state.symbols[symbol]
        symbols_payload[symbol] = {
            "lots": [
                {
                    "qty": _serialize_decimal(lot.qty),
                    "unit_cost": _serialize_decimal(lot.unit_cost),
                    "opened_at": ensure_utc(lot.opened_at).isoformat(),
                }
                for lot in symbol_ledger.lots
            ],
            "realized_pnl": _serialize_decimal(symbol_ledger.realized_pnl),
        }

    fees_payload = {
        currency: _serialize_decimal(state.fees_by_currency[currency])
        for currency in sorted(state.fees_by_currency)
    }

    return json.dumps(
        {"symbols": symbols_payload, "fees_by_currency": fees_payload},
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_ledger_state(payload: str) -> LedgerState:
    raw = json.loads(payload)
    symbols_raw = raw.get("symbols", {})
    fees_raw = raw.get("fees_by_currency", {})

    symbols: dict[str, SymbolLedger] = {}
    for symbol in sorted(symbols_raw):
        symbol_payload = symbols_raw[symbol]
        lots = tuple(
            PositionLot(
                symbol=symbol,
                qty=Decimal(str(lot["qty"])),
                unit_cost=Decimal(str(lot["unit_cost"])),
                opened_at=ensure_utc(datetime.fromisoformat(str(lot["opened_at"]))),
            )
            for lot in symbol_payload.get("lots", [])
        )
        symbols[symbol] = SymbolLedger(
            symbol=symbol,
            lots=lots,
            realized_pnl=Decimal(str(symbol_payload.get("realized_pnl", "0"))),
        )

    fees_by_currency = {
        str(currency): Decimal(str(amount)) for currency, amount in sorted(fees_raw.items())
    }
    return LedgerState(symbols=symbols, fees_by_currency=fees_by_currency)


def compute_realized_pnl(
    state: LedgerState,
    policy_resolver: Callable[[str], MoneyMathPolicy] | None = None,
) -> Decimal:
    total = sum((symbol.realized_pnl for symbol in state.symbols.values()), Decimal("0"))
    policy = DEFAULT_MONEY_POLICY
    if policy_resolver is not None and state.symbols:
        first_symbol = next(iter(state.symbols))
        policy = policy_resolver(first_symbol)
    return round_quote(total, policy)


def compute_unrealized_pnl(
    state: LedgerState,
    mark_prices: dict[str, Decimal],
    policy_resolver: Callable[[str], MoneyMathPolicy] | None = None,
) -> Decimal:
    total = Decimal("0")
    for symbol, symbol_state in state.symbols.items():
        mark = mark_prices.get(symbol)
        if mark is None:
            continue
        policy = policy_resolver(symbol) if policy_resolver is not None else DEFAULT_MONEY_POLICY
        mark = round_price(mark, policy)
        for lot in symbol_state.lots:
            total = round_quote(total + ((mark - lot.unit_cost) * lot.qty), policy)
    policy = DEFAULT_MONEY_POLICY
    if policy_resolver is not None and state.symbols:
        first_symbol = next(iter(state.symbols))
        policy = policy_resolver(first_symbol)
    return round_quote(total, policy)


def equity_curve(points: list[tuple[datetime, Decimal]]) -> list[tuple[datetime, Decimal]]:
    return sorted(points, key=lambda point: point[0])


def compute_max_drawdown(points: list[EquityPoint]) -> Decimal:
    peak: Decimal | None = None
    max_dd = Decimal("0")
    for point in sorted(points, key=lambda row: ensure_utc(row.ts)):
        if peak is None or point.equity_try > peak:
            peak = point.equity_try
            continue
        if peak <= 0:
            continue
        drawdown = (peak - point.equity_try) / peak
        if drawdown > max_dd:
            max_dd = drawdown
    return max_dd


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)
