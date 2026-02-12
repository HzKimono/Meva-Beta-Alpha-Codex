from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.accounting import Position, TradeFill
from btcbot.domain.models import OrderSide, normalize_symbol
from btcbot.domain.symbols import quote_currency
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)


class AccountingService:
    def __init__(self, exchange: ExchangeClient, state_store: StateStore) -> None:
        self.exchange = exchange
        self.state_store = state_store

    def refresh(self, symbols: list[str], mark_prices: dict[str, Decimal]) -> int:
        inserted = 0
        get_recent_fills = getattr(self.exchange, "get_recent_fills", None)
        for symbol in symbols:
            symbol_fills = get_recent_fills(symbol) if callable(get_recent_fills) else []
            for fill in symbol_fills:
                if self.state_store.save_fill(fill):
                    inserted += 1
                    self._apply_fill(fill)

        get_positions = getattr(self.state_store, "get_positions", None)
        positions = get_positions() if callable(get_positions) else []
        for position in positions:
            mark = mark_prices.get(normalize_symbol(position.symbol), Decimal("0"))
            unrealized = (mark - position.avg_cost) * position.qty if mark > 0 else Decimal("0")
            position.unrealized_pnl = unrealized
            position.updated_at = datetime.now(UTC)
            save_position = getattr(self.state_store, "save_position", None)
            if callable(save_position):
                save_position(position)
        return inserted

    def _apply_fill(self, fill: TradeFill) -> None:
        symbol = normalize_symbol(fill.symbol)
        get_position = getattr(self.state_store, "get_position", None)
        loaded = get_position(symbol) if callable(get_position) else None
        position = loaded or Position(
            symbol=symbol,
            qty=Decimal("0"),
            avg_cost=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("0"),
            updated_at=datetime.now(UTC),
        )

        quote = quote_currency(fill.symbol)
        fee_quote = fill.fee if fill.fee_currency.upper() == quote else Decimal("0")
        if fill.fee_currency.upper() != quote:
            logger.warning(
                "Ignoring non-quote fee currency in accounting",
                extra={
                    "extra": {
                        "symbol": symbol,
                        "fee_currency": fill.fee_currency,
                    }
                },
            )

        if fill.side == OrderSide.BUY:
            total_cost = (position.qty * position.avg_cost) + (fill.qty * fill.price) + fee_quote
            new_qty = position.qty + fill.qty
            position.avg_cost = (total_cost / new_qty) if new_qty > 0 else Decimal("0")
            position.qty = new_qty
            position.fees_paid += fee_quote
        else:
            sell_qty = min(position.qty, fill.qty)
            fee_used = (fee_quote * (sell_qty / fill.qty)) if fill.qty > 0 else Decimal("0")
            position.realized_pnl += (sell_qty * (fill.price - position.avg_cost)) - fee_used
            position.qty -= sell_qty
            position.fees_paid += fee_used
            if position.qty <= 0:
                position.qty = Decimal("0")
                position.avg_cost = Decimal("0")
        position.updated_at = fill.ts
        save_position = getattr(self.state_store, "save_position", None)
        if callable(save_position):
            save_position(position)

    def get_positions(self) -> list[Position]:
        get_positions = getattr(self.state_store, "get_positions", None)
        return get_positions() if callable(get_positions) else []

    def compute_total_pnl(self) -> Decimal:
        return sum(
            (pos.realized_pnl + pos.unrealized_pnl for pos in self.get_positions()), Decimal("0")
        )

    def compute_cycle_pnl_delta(self) -> Decimal:
        """Deprecated alias for backwards compatibility; use compute_total_pnl."""

        return self.compute_total_pnl()
