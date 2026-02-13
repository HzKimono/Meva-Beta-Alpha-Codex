from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal

from btcbot.domain.ledger import (
    LedgerEvent,
    LedgerEventType,
    LedgerState,
    apply_events,
    compute_realized_pnl,
    compute_unrealized_pnl,
)
from btcbot.domain.models import normalize_symbol
from btcbot.domain.stage4 import Fill
from btcbot.services.state_store import StateStore


@dataclass(frozen=True)
class LedgerIngestResult:
    events_attempted: int
    events_inserted: int
    events_ignored: int


@dataclass(frozen=True)
class SymbolPnlBreakdown:
    symbol: str
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    position_qty: Decimal


@dataclass(frozen=True)
class PnlReport:
    realized_pnl_total: Decimal
    unrealized_pnl_total: Decimal
    fees_total_by_currency: dict[str, Decimal]
    per_symbol: list[SymbolPnlBreakdown]
    equity_estimate: Decimal


class LedgerService:
    def __init__(self, state_store: StateStore, logger: logging.Logger) -> None:
        self.state_store = state_store
        self.logger = logger

    def ingest_exchange_updates(self, fills: list[Fill]) -> LedgerIngestResult:
        events: list[LedgerEvent] = []
        for fill in fills:
            symbol = normalize_symbol(fill.symbol)
            fill_event_id = f"fill:{fill.fill_id}"
            events.append(
                LedgerEvent(
                    event_id=fill_event_id,
                    ts=fill.ts.astimezone(UTC),
                    symbol=symbol,
                    type=LedgerEventType.FILL,
                    side=fill.side.upper(),
                    qty=fill.qty,
                    price=fill.price,
                    fee=None,
                    fee_currency=None,
                    exchange_trade_id=fill.fill_id,
                    exchange_order_id=fill.order_id,
                    client_order_id=None,
                    meta={"source": "stage4_accounting_fill"},
                )
            )
            if fill.fee > 0:
                events.append(
                    LedgerEvent(
                        event_id=f"fee:{fill.fill_id}",
                        ts=fill.ts.astimezone(UTC),
                        symbol=symbol,
                        type=LedgerEventType.FEE,
                        side=None,
                        qty=Decimal("0"),
                        price=None,
                        fee=fill.fee,
                        fee_currency=fill.fee_asset.upper(),
                        exchange_trade_id=f"fee:{fill.fill_id}",
                        exchange_order_id=fill.order_id,
                        client_order_id=None,
                        meta={
                            "linked_fill_id": fill.fill_id,
                            "source": "stage4_accounting_fill_fee",
                        },
                    )
                )

        append = self.state_store.append_ledger_events(events)
        return LedgerIngestResult(
            events_attempted=append.attempted,
            events_inserted=append.inserted,
            events_ignored=append.ignored,
        )

    def report(
        self,
        mark_prices: dict[str, Decimal],
        cash_try: Decimal = Decimal("0"),
    ) -> PnlReport:
        events = self.state_store.load_ledger_events()
        state = apply_events(LedgerState(), events)

        realized = compute_realized_pnl(state)
        normalized_marks = {
            normalize_symbol(symbol): value for symbol, value in mark_prices.items()
        }
        unrealized = compute_unrealized_pnl(state, normalized_marks)

        per_symbol: list[SymbolPnlBreakdown] = []
        for symbol, ledger in sorted(state.symbols.items()):
            qty = sum((lot.qty for lot in ledger.lots), Decimal("0"))
            symbol_unrealized = Decimal("0")
            mark = normalized_marks.get(symbol)
            if mark is not None:
                symbol_unrealized = sum(
                    ((mark - lot.unit_cost) * lot.qty for lot in ledger.lots), Decimal("0")
                )
            per_symbol.append(
                SymbolPnlBreakdown(
                    symbol=symbol,
                    realized_pnl=ledger.realized_pnl,
                    unrealized_pnl=symbol_unrealized,
                    position_qty=qty,
                )
            )

        mtm = Decimal("0")
        for symbol, ledger in state.symbols.items():
            mark = normalized_marks.get(symbol)
            if mark is None:
                continue
            mtm += sum((lot.qty * mark for lot in ledger.lots), Decimal("0"))

        return PnlReport(
            realized_pnl_total=realized,
            unrealized_pnl_total=unrealized,
            fees_total_by_currency=dict(state.fees_by_currency),
            per_symbol=per_symbol,
            equity_estimate=cash_try + mtm,
        )
