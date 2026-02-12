from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.adapters.exchange_stage4 import ExchangeClientStage4
from btcbot.domain.models import normalize_symbol
from btcbot.domain.stage4 import Fill, PnLSnapshot, Position
from btcbot.services.state_store import StateStore


class AccountingIntegrityError(RuntimeError):
    pass


class AccountingService:
    def __init__(
        self,
        exchange: ExchangeClientStage4,
        state_store: StateStore,
        *,
        lookback_minutes: int = 30,
    ) -> None:
        self.exchange = exchange
        self.state_store = state_store
        self.lookback_minutes = lookback_minutes

    def fetch_new_fills(self, symbol: str) -> list[Fill]:
        cursor_key = f"fills_cursor:{normalize_symbol(symbol)}"
        since_ms: int | None = None
        lookback_ms = self.lookback_minutes * 60 * 1000
        stored_cursor = self.state_store.get_cursor(cursor_key)
        if stored_cursor is not None:
            since_ms = max(0, int(stored_cursor) - lookback_ms)

        incoming = self.exchange.get_recent_fills(symbol, since_ms=since_ms)
        if since_ms is None:
            since_dt = datetime.now(UTC) - timedelta(minutes=self.lookback_minutes)
            incoming = [fill for fill in incoming if fill.ts >= since_dt]

        new_fills: list[Fill] = []
        max_ts_ms = since_ms or 0
        for trade_fill in incoming:
            fill_id = (trade_fill.fill_id or "").strip()
            if not fill_id:
                ts_ms = int(trade_fill.ts.timestamp() * 1000)
                fill_id = (
                    f"{trade_fill.order_id}:{ts_ms}:{trade_fill.price}:"
                    f"{trade_fill.qty}:{trade_fill.side.value}"
                )
            fill = Fill(
                fill_id=fill_id,
                order_id=trade_fill.order_id,
                symbol=trade_fill.symbol,
                side=trade_fill.side.value,
                price=trade_fill.price,
                qty=trade_fill.qty,
                fee=trade_fill.fee,
                fee_asset=trade_fill.fee_currency,
                ts=trade_fill.ts,
            )
            if self.state_store.save_stage4_fill(fill):
                new_fills.append(fill)
            max_ts_ms = max(max_ts_ms, int(trade_fill.ts.timestamp() * 1000))

        if max_ts_ms > 0:
            self.state_store.set_cursor(cursor_key, str(max_ts_ms))
        return new_fills

    def apply_fills(
        self,
        fills: list[Fill],
        *,
        mark_prices: dict[str, Decimal],
        try_cash: Decimal,
    ) -> PnLSnapshot:
        fee_notes: list[str] = []
        for fill in fills:
            position = self.state_store.get_stage4_position(fill.symbol) or Position(
                symbol=fill.symbol,
                qty=Decimal("0"),
                avg_cost_try=Decimal("0"),
                realized_pnl_try=Decimal("0"),
                last_update_ts=fill.ts,
            )

            fee_try = Decimal("0")
            if fill.fee_asset.upper() == "TRY":
                fee_try = fill.fee
            else:
                fee_notes.append(f"fee_conversion_missing:{fill.fill_id}:{fill.fee_asset}")

            if fill.side.lower() == "buy":
                total_cost = (
                    (position.avg_cost_try * position.qty) + (fill.price * fill.qty) + fee_try
                )
                new_qty = position.qty + fill.qty
                position = Position(
                    symbol=position.symbol,
                    qty=new_qty,
                    avg_cost_try=(total_cost / new_qty) if new_qty > 0 else Decimal("0"),
                    realized_pnl_try=position.realized_pnl_try,
                    last_update_ts=fill.ts,
                )
            else:
                if fill.qty > position.qty:
                    raise AccountingIntegrityError(
                        "oversell_detected "
                        f"symbol={fill.symbol} "
                        f"position_qty={position.qty} "
                        f"fill_qty={fill.qty}"
                    )
                realized = (fill.price - position.avg_cost_try) * fill.qty - fee_try
                remaining = position.qty - fill.qty
                position = Position(
                    symbol=position.symbol,
                    qty=remaining,
                    avg_cost_try=(position.avg_cost_try if remaining > 0 else Decimal("0")),
                    realized_pnl_try=position.realized_pnl_try + realized,
                    last_update_ts=fill.ts,
                )
            self.state_store.save_stage4_position(position)

        positions = self.state_store.list_stage4_positions()
        realized_total = sum((position.realized_pnl_try for position in positions), Decimal("0"))

        total_equity = try_cash
        for position in positions:
            mark = mark_prices.get(normalize_symbol(position.symbol), position.avg_cost_try)
            total_equity += position.qty * mark

        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        baseline = self.state_store.realized_total_at_day_start(day_start)
        realized_today = realized_total - baseline
        drawdown = self.state_store.compute_drawdown_pct(total_equity)

        snapshot = PnLSnapshot(
            total_equity_try=total_equity,
            realized_today_try=realized_today,
            drawdown_pct=drawdown,
            ts=datetime.now(UTC),
            realized_total_try=realized_total,
        )
        self.state_store.save_stage4_pnl_snapshot(snapshot)
        if fee_notes:
            self.state_store.record_cycle_audit(
                cycle_id=f"accounting-{int(snapshot.ts.timestamp())}",
                counts={"fills": len(fills)},
                decisions=fee_notes,
            )
        return snapshot
