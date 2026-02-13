from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


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


def _sort_events(events: list[LedgerEvent]) -> list[LedgerEvent]:
    return sorted(events, key=lambda event: (event.ts, event.event_id))


def apply_events(state: LedgerState, events: list[LedgerEvent]) -> LedgerState:
    symbol_state = dict(state.symbols)
    fees = dict(state.fees_by_currency)

    for event in _sort_events(events):
        current = symbol_state.get(event.symbol, SymbolLedger(symbol=event.symbol))
        lots = list(current.lots)
        realized = current.realized_pnl

        if (
            event.type == LedgerEventType.FILL
            and event.price is not None
            and event.side is not None
        ):
            if event.side.upper() == "BUY":
                lots.append(
                    PositionLot(
                        symbol=event.symbol,
                        qty=event.qty,
                        unit_cost=event.price,
                        opened_at=event.ts,
                    )
                )
            elif event.side.upper() == "SELL":
                remaining = event.qty
                while remaining > 0 and lots:
                    lot = lots[0]
                    matched = min(remaining, lot.qty)
                    realized += (event.price - lot.unit_cost) * matched
                    remaining -= matched
                    leftover = lot.qty - matched
                    if leftover <= 0:
                        lots.pop(0)
                    else:
                        lots[0] = PositionLot(
                            symbol=lot.symbol,
                            qty=leftover,
                            unit_cost=lot.unit_cost,
                            opened_at=lot.opened_at,
                        )
        if event.type == LedgerEventType.FEE and event.fee is not None and event.fee_currency:
            currency = event.fee_currency.upper()
            fees[currency] = fees.get(currency, Decimal("0")) + event.fee
            if currency == "TRY":
                realized -= event.fee

        if event.type == LedgerEventType.ADJUSTMENT and event.fee is not None:
            realized += event.fee

        symbol_state[event.symbol] = SymbolLedger(
            symbol=event.symbol,
            lots=tuple(lots),
            realized_pnl=realized,
        )

    return LedgerState(symbols=symbol_state, fees_by_currency=fees)


def compute_realized_pnl(state: LedgerState) -> Decimal:
    return sum((symbol.realized_pnl for symbol in state.symbols.values()), Decimal("0"))


def compute_unrealized_pnl(state: LedgerState, mark_prices: dict[str, Decimal]) -> Decimal:
    total = Decimal("0")
    for symbol, symbol_state in state.symbols.items():
        mark = mark_prices.get(symbol)
        if mark is None:
            continue
        for lot in symbol_state.lots:
            total += (mark - lot.unit_cost) * lot.qty
    return total


def equity_curve(points: list[tuple[datetime, Decimal]]) -> list[tuple[datetime, Decimal]]:
    return sorted(points, key=lambda point: point[0])


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)
