from __future__ import annotations

from collections import defaultdict
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

    overall_weighted_slippage_sum = Decimal("0")
    overall_slippage_qty_sum = Decimal("0")
    per_symbol_total_fills: dict[str, int] = defaultdict(int)
    per_symbol_weighted_slippage_sum: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    per_symbol_slippage_qty_sum: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for fill in fills:
        symbol = normalize_symbol(fill.symbol)
        per_symbol_total_fills[symbol] += 1

        mark = market_marks.get(symbol)
        if mark is None or mark <= 0 or fill.qty <= 0:
            continue

        raw_side = getattr(fill.side, "value", fill.side)
        side = str(raw_side).lower()
        if side == "buy":
            bps = ((fill.price - mark) / mark) * Decimal("10000")
        elif side == "sell":
            bps = ((mark - fill.price) / mark) * Decimal("10000")
        else:
            continue

        weighted_bps = bps * fill.qty
        overall_weighted_slippage_sum += weighted_bps
        overall_slippage_qty_sum += fill.qty
        per_symbol_weighted_slippage_sum[symbol] += weighted_bps
        per_symbol_slippage_qty_sum[symbol] += fill.qty

    slippage_bps_avg = None
    if overall_slippage_qty_sum > 0:
        slippage_bps_avg = overall_weighted_slippage_sum / overall_slippage_qty_sum

    per_symbol = []
    for symbol in sorted(per_symbol_total_fills):
        symbol_slippage_avg = None
        symbol_qty_sum = per_symbol_slippage_qty_sum[symbol]
        if symbol_qty_sum > 0:
            symbol_slippage_avg = per_symbol_weighted_slippage_sum[symbol] / symbol_qty_sum
        symbol_fills_count = per_symbol_total_fills[symbol]
        per_symbol.append(
            PerSymbolExecutionQuality(
                symbol=symbol,
                fills_count=symbol_fills_count,
                slippage_bps_avg=symbol_slippage_avg,
                fills_per_submitted_order=(Decimal(symbol_fills_count) / Decimal(orders_submitted))
                if orders_submitted > 0
                else None,
            )
        )

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
