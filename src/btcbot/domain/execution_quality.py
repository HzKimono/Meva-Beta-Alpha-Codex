from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.models import normalize_symbol
from btcbot.domain.stage4 import Fill


@dataclass(frozen=True)
class PerSymbolExecutionQuality:
    symbol: str
    fills_count: int
    slippage_bps_avg: Decimal | None
    fills_per_submitted_order: Decimal | None


@dataclass(frozen=True)
class ExecutionQualitySnapshot:
    fills_count: int
    orders_submitted: int
    orders_canceled: int
    rejects_count: int
    # Number of fills observed per submitted order in the cycle window.
    # This is not an order-level fill ratio and may exceed 1.0 for partial fills.
    fills_per_submitted_order: Decimal
    avg_time_to_fill: float | None
    slippage_bps_avg: Decimal | None
    per_symbol: list[PerSymbolExecutionQuality]


def compute_execution_quality(
    cycle_data: dict[str, int],
    fills: list[Fill],
    market_marks: dict[str, Decimal],
) -> ExecutionQualitySnapshot:
    orders_submitted = int(cycle_data.get("orders_submitted", 0))
    orders_canceled = int(cycle_data.get("orders_canceled", 0))
    rejects_count = int(cycle_data.get("rejects_count", 0))

    fills_count = len(fills)
    fills_per_submitted_order = Decimal("0")
    if orders_submitted > 0:
        fills_per_submitted_order = Decimal(fills_count) / Decimal(orders_submitted)

    weighted_slippage_numerator = Decimal("0")
    weighted_slippage_qty = Decimal("0")
    per_symbol_total_fills: dict[str, int] = {}
    per_symbol_weighted_slippage_numerator: dict[str, Decimal] = {}
    per_symbol_weighted_slippage_qty: dict[str, Decimal] = {}
    for fill in fills:
        symbol = normalize_symbol(fill.symbol)
        per_symbol_total_fills[symbol] = per_symbol_total_fills.get(symbol, 0) + 1

        mark = market_marks.get(symbol)
        if mark is None or mark <= 0:
            continue

        side = fill.side.lower()
        if side == "buy":
            bps = ((fill.price - mark) / mark) * Decimal("10000")
        elif side == "sell":
            bps = ((mark - fill.price) / mark) * Decimal("10000")
        else:
            continue

        if fill.qty <= 0:
            continue

        weighted_slippage_numerator += bps * fill.qty
        weighted_slippage_qty += fill.qty
        per_symbol_weighted_slippage_numerator[symbol] = per_symbol_weighted_slippage_numerator.get(
            symbol, Decimal("0")
        ) + (bps * fill.qty)
        per_symbol_weighted_slippage_qty[symbol] = (
            per_symbol_weighted_slippage_qty.get(symbol, Decimal("0")) + fill.qty
        )

    slippage_bps_avg = None
    if weighted_slippage_qty > 0:
        slippage_bps_avg = weighted_slippage_numerator / weighted_slippage_qty

    per_symbol = [
        PerSymbolExecutionQuality(
            symbol=symbol,
            fills_count=total_fills,
            slippage_bps_avg=(
                per_symbol_weighted_slippage_numerator[symbol]
                / per_symbol_weighted_slippage_qty[symbol]
                if per_symbol_weighted_slippage_qty.get(symbol, Decimal("0")) > 0
                else None
            ),
            fills_per_submitted_order=(Decimal(total_fills) / Decimal(orders_submitted))
            if orders_submitted > 0
            else None,
        )
        for symbol, total_fills in sorted(per_symbol_total_fills.items())
    ]

    return ExecutionQualitySnapshot(
        fills_count=fills_count,
        orders_submitted=orders_submitted,
        orders_canceled=orders_canceled,
        rejects_count=rejects_count,
        fills_per_submitted_order=fills_per_submitted_order,
        avg_time_to_fill=None,
        slippage_bps_avg=slippage_bps_avg,
        per_symbol=per_symbol,
    )
