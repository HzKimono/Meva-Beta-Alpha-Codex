from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.exchange_stage4 import ExchangeClientStage4
from btcbot.domain.models import normalize_symbol
from btcbot.domain.stage4 import Fill, PnLSnapshot, Position
from btcbot.services.state_store import StateStore


class AccountingIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchFillsResult:
    fills: list[Fill]
    fills_seen: int
    fills_deduped: int
    cursor_after: str | None
    last_seen_fill_id: str | None
    last_seen_ts_ms: int | None


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
        self.last_applied_fills_count = 0
        self.last_apply_stats_by_symbol: dict[str, dict[str, int]] = {}

    def _convert_fee_to_try(
        self,
        *,
        fee_asset: str,
        fee_amount: Decimal,
        mark_prices: dict[str, Decimal],
    ) -> tuple[Decimal, str | None]:
        currency = fee_asset.upper()
        if fee_amount <= 0:
            return Decimal("0"), None
        if currency == "TRY":
            return fee_amount, None

        normalized_marks = {
            normalize_symbol(symbol): Decimal(str(price)) for symbol, price in mark_prices.items()
        }
        direct = normalized_marks.get(normalize_symbol(f"{currency}TRY"))
        if direct is not None and direct > 0:
            return fee_amount * direct, None

        inverse = normalized_marks.get(normalize_symbol(f"TRY{currency}"))
        if inverse is not None and inverse > 0:
            return fee_amount / inverse, None

        return Decimal("0"), f"missing_fee_conversion:{currency}"

    def fetch_new_fills(self, symbol: str) -> FetchFillsResult:
        """Fetch fills with a lookback while preserving idempotency invariants.

        Invariants:
        - fill_id is the stable dedupe key and can be applied at most once.
        - cursor_after is monotonic (never below prior cursor) and only advances to max seen ts.
        - fee events are keyed by the same fill_id downstream (ledger_service event_id = fee:{fill_id}).
        """
        cursor_key = f"fills_cursor:{normalize_symbol(symbol)}"
        since_ms: int | None = None
        lookback_ms = self.lookback_minutes * 60 * 1000
        stored_cursor = self.state_store.get_cursor(cursor_key)
        cursor_floor_ms = int(stored_cursor) if stored_cursor is not None else 0
        if stored_cursor is not None:
            since_ms = max(0, cursor_floor_ms - lookback_ms)
        else:
            since_ms = max(0, int(datetime.now(UTC).timestamp() * 1000) - lookback_ms)

        incoming = self.exchange.get_recent_fills(symbol, since_ms=since_ms)
        since_dt = datetime.fromtimestamp(since_ms / 1000, tz=UTC)
        incoming = [fill for fill in incoming if fill.ts >= since_dt]

        fills: list[Fill] = []
        seen_fill_ids: set[str] = set()
        fills_seen = 0
        fills_deduped = 0
        max_ts_ms = cursor_floor_ms
        last_seen_fill_id: str | None = None
        last_seen_ts_ms: int | None = None
        for trade_fill in sorted(incoming, key=lambda item: (item.ts, str(item.fill_id or ""))):
            fills_seen += 1
            fill_id = (trade_fill.fill_id or "").strip()
            if not fill_id:
                ts_ms = int(trade_fill.ts.timestamp() * 1000)
                fill_id = (
                    f"{trade_fill.order_id}:{ts_ms}:{trade_fill.price}:"
                    f"{trade_fill.qty}:{trade_fill.side.value}"
                )
            if fill_id in seen_fill_ids:
                fills_deduped += 1
                continue
            seen_fill_ids.add(fill_id)
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
            fills.append(fill)
            ts_ms = int(trade_fill.ts.timestamp() * 1000)
            max_ts_ms = max(max_ts_ms, ts_ms)
            if last_seen_ts_ms is None or ts_ms >= last_seen_ts_ms:
                last_seen_fill_id = fill_id
                last_seen_ts_ms = ts_ms

        return FetchFillsResult(
            fills=fills,
            fills_seen=fills_seen,
            fills_deduped=fills_deduped,
            cursor_after=(str(max_ts_ms) if max_ts_ms > 0 else None),
            last_seen_fill_id=last_seen_fill_id,
            last_seen_ts_ms=last_seen_ts_ms,
        )

    def apply_fills(
        self,
        fills: list[Fill],
        *,
        mark_prices: dict[str, Decimal],
        try_cash: Decimal,
    ) -> PnLSnapshot:
        fee_notes: list[str] = []
        applied_fills_count = 0
        stats_by_symbol: dict[str, dict[str, int]] = {}
        for fill in fills:
            normalized_symbol = normalize_symbol(fill.symbol)
            stats = stats_by_symbol.setdefault(normalized_symbol, {"new": 0, "deduped": 0})
            if not self.state_store.mark_fill_applied(fill.fill_id):
                stats["deduped"] += 1
                continue
            stats["new"] += 1
            applied_fills_count += 1
            self.state_store.save_stage4_fill(fill)
            position = self.state_store.get_stage4_position(fill.symbol) or Position(
                symbol=fill.symbol,
                qty=Decimal("0"),
                avg_cost_try=Decimal("0"),
                realized_pnl_try=Decimal("0"),
                last_update_ts=fill.ts,
            )

            fee_try, fee_note = self._convert_fee_to_try(
                fee_asset=fill.fee_asset,
                fee_amount=fill.fee,
                mark_prices=mark_prices,
            )
            if fee_note is not None:
                fee_notes.append(f"{fee_note}:{fill.fill_id}")

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
        self.last_applied_fills_count = applied_fills_count
        self.last_apply_stats_by_symbol = stats_by_symbol
        return snapshot
