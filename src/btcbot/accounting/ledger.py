from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.accounting.models import (
    AccountingEventType,
    AccountingLedgerEvent,
    PortfolioAccountingState,
    PositionLot,
    SymbolPnlState,
    quantize_money,
    quantize_qty,
)


@dataclass(frozen=True)
class _SymbolState:
    lots: tuple[PositionLot, ...]
    realized_pnl_try: Decimal
    fees_try: Decimal
    funding_cost_try: Decimal
    slippage_try: Decimal


class AccountingLedger:
    """Deterministic, append-only event replay for self-financing accounting."""

    def recompute(
        self,
        *,
        events: list[AccountingLedgerEvent],
        as_of: datetime,
        mark_prices_try: dict[str, Decimal],
        initial_trading_capital_try: Decimal = Decimal("0"),
        initial_treasury_try: Decimal = Decimal("0"),
    ) -> PortfolioAccountingState:
        effective_as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        deduped = self._dedupe_events(events)
        ordered = sorted(
            (event for event in deduped if event.canonical_ts() <= effective_as_of),
            key=lambda item: (item.canonical_ts(), item.event_id),
        )

        symbol_state: dict[str, _SymbolState] = {}
        balances_try: dict[str, Decimal] = {"TRY": quantize_money(initial_trading_capital_try)}
        locked_try: dict[str, Decimal] = {}
        treasury_try = quantize_money(initial_treasury_try)

        for event in ordered:
            symbol = (event.symbol or "").upper()
            state = symbol_state.get(
                symbol,
                _SymbolState(
                    lots=(),
                    realized_pnl_try=Decimal("0"),
                    fees_try=Decimal("0"),
                    funding_cost_try=Decimal("0"),
                    slippage_try=Decimal("0"),
                ),
            )
            lots = list(state.lots)
            realized = state.realized_pnl_try
            fees = state.fees_try
            funding = state.funding_cost_try
            slippage = state.slippage_try

            if event.type == AccountingEventType.FILL_RECORDED and symbol:
                qty = quantize_qty(abs(event.qty))
                price = event.price_try or Decimal("0")
                side = str(event.side or "").upper()
                notional_try = quantize_money(qty * price)
                if side == "BUY":
                    lots.append(
                        PositionLot(
                            qty=qty,
                            unit_cost_try=quantize_money(price),
                            opened_at=event.canonical_ts(),
                        )
                    )
                    balances_try["TRY"] = quantize_money(
                        balances_try.get("TRY", Decimal("0")) - notional_try
                    )
                elif side == "SELL":
                    remaining = qty
                    while remaining > 0 and lots:
                        lot = lots[0]
                        matched = min(remaining, lot.qty)
                        realized += quantize_money((price - lot.unit_cost_try) * matched)
                        remaining = quantize_qty(remaining - matched)
                        left = quantize_qty(lot.qty - matched)
                        if left <= 0:
                            lots.pop(0)
                        else:
                            lots[0] = PositionLot(
                                qty=left,
                                unit_cost_try=lot.unit_cost_try,
                                opened_at=lot.opened_at,
                            )
                    if remaining > 0:
                        msg = f"oversell symbol={symbol} qty={remaining} event_id={event.event_id}"
                        raise ValueError(msg)
                    balances_try["TRY"] = quantize_money(
                        balances_try.get("TRY", Decimal("0")) + notional_try
                    )

            if event.type == AccountingEventType.FEE_RECORDED:
                amount = quantize_money(abs(event.amount_try or Decimal("0")))
                fees += amount
                balances_try["TRY"] = quantize_money(balances_try.get("TRY", Decimal("0")) - amount)

            if event.type == AccountingEventType.FUNDING_COST_RECORDED:
                amount = quantize_money(abs(event.amount_try or Decimal("0")))
                funding += amount
                balances_try["TRY"] = quantize_money(balances_try.get("TRY", Decimal("0")) - amount)

            if event.type == AccountingEventType.SLIPPAGE_RECORDED:
                slippage += quantize_money(abs(event.amount_try or Decimal("0")))

            if event.type == AccountingEventType.TRANSFER:
                amount = quantize_money(event.amount_try or Decimal("0"))
                balances_try["TRY"] = quantize_money(balances_try.get("TRY", Decimal("0")) + amount)

            if event.type == AccountingEventType.REBALANCE:
                amount = quantize_money(event.amount_try or Decimal("0"))
                treasury_try = quantize_money(treasury_try + amount)
                balances_try["TRY"] = quantize_money(balances_try.get("TRY", Decimal("0")) - amount)

            if event.type == AccountingEventType.WITHDRAWAL:
                amount = quantize_money(abs(event.amount_try or Decimal("0")))
                balances_try["TRY"] = quantize_money(balances_try.get("TRY", Decimal("0")) - amount)

            if symbol:
                symbol_state[symbol] = _SymbolState(
                    lots=tuple(lots),
                    realized_pnl_try=quantize_money(realized),
                    fees_try=quantize_money(fees),
                    funding_cost_try=quantize_money(funding),
                    slippage_try=quantize_money(slippage),
                )

        symbols: dict[str, SymbolPnlState] = {}
        unrealized_total = Decimal("0")
        realized_total = Decimal("0")
        fees_total = Decimal("0")
        funding_total = Decimal("0")
        slippage_total = Decimal("0")

        for symbol, state in sorted(symbol_state.items()):
            qty = quantize_qty(sum((lot.qty for lot in state.lots), Decimal("0")))
            avg_cost = Decimal("0")
            if qty > 0:
                avg_cost = quantize_money(
                    sum((lot.qty * lot.unit_cost_try for lot in state.lots), Decimal("0")) / qty
                )
            mark = mark_prices_try.get(symbol)
            symbol_unrealized = Decimal("0")
            if mark is not None:
                symbol_unrealized = quantize_money(
                    sum(((mark - lot.unit_cost_try) * lot.qty for lot in state.lots), Decimal("0"))
                )

            symbols[symbol] = SymbolPnlState(
                symbol=symbol,
                qty=qty,
                avg_cost_try=avg_cost,
                realized_pnl_try=state.realized_pnl_try,
                unrealized_pnl_try=symbol_unrealized,
                fees_try=state.fees_try,
                funding_cost_try=state.funding_cost_try,
                slippage_try=state.slippage_try,
            )
            realized_total += state.realized_pnl_try
            unrealized_total += symbol_unrealized
            fees_total += state.fees_try
            funding_total += state.funding_cost_try
            slippage_total += state.slippage_try

        trading_capital_try = quantize_money(balances_try.get("TRY", Decimal("0")))

        return PortfolioAccountingState(
            as_of=effective_as_of,
            balances_try={k: quantize_money(v) for k, v in balances_try.items()},
            locked_try={k: quantize_money(v) for k, v in locked_try.items()},
            treasury_try=treasury_try,
            trading_capital_try=trading_capital_try,
            realized_pnl_try=quantize_money(realized_total),
            unrealized_pnl_try=quantize_money(unrealized_total),
            fees_try=quantize_money(fees_total),
            funding_cost_try=quantize_money(funding_total),
            slippage_try=quantize_money(slippage_total),
            symbols=symbols,
        )

    def _dedupe_events(self, events: list[AccountingLedgerEvent]) -> list[AccountingLedgerEvent]:
        deduped: dict[str, AccountingLedgerEvent] = {}

        def _sort_key(event: AccountingLedgerEvent) -> tuple:
            return (
                event.canonical_ts(),
                str(event.type),
                str(event.symbol or ""),
                str(event.side or ""),
                str(event.qty),
                str(event.price_try or Decimal("0")),
                str(event.amount_try or Decimal("0")),
                str(event.fee_currency or ""),
                str(event.reference_id or ""),
                tuple(sorted((str(key), str(value)) for key, value in event.metadata.items())),
            )

        for event in events:
            current = deduped.get(event.event_id)
            if current is None or _sort_key(event) < _sort_key(current):
                deduped[event.event_id] = event
        return list(deduped.values())
