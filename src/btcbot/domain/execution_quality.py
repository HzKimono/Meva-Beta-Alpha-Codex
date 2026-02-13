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


@dataclass(frozen=True)
class ExecutionQualitySnapshot:
    fills_count: int
    orders_submitted: int
    orders_canceled: int
    rejects_count: int
    fill_rate: Decimal
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
    fill_rate = Decimal("0")
    if orders_submitted > 0:
        fill_rate = Decimal(fills_count) / Decimal(orders_submitted)

    slippages: list[Decimal] = []
    per_symbol_map: dict[str, list[Decimal]] = {}
    for fill in fills:
        mark = market_marks.get(normalize_symbol(fill.symbol))
        if mark is None or mark <= 0:
            continue
        if fill.side.lower() == "buy":
            bps = ((fill.price - mark) / mark) * Decimal("10000")
        else:
            bps = ((mark - fill.price) / mark) * Decimal("10000")
        slippages.append(bps)
        per_symbol_map.setdefault(normalize_symbol(fill.symbol), []).append(bps)

    slippage_bps_avg = None
    if slippages:
        slippage_bps_avg = sum(slippages, Decimal("0")) / Decimal(len(slippages))

    per_symbol = [
        PerSymbolExecutionQuality(
            symbol=symbol,
            fills_count=len(values),
            slippage_bps_avg=(sum(values, Decimal("0")) / Decimal(len(values))),
        )
        for symbol, values in sorted(per_symbol_map.items())
    ]

    return ExecutionQualitySnapshot(
        fills_count=fills_count,
        orders_submitted=orders_submitted,
        orders_canceled=orders_canceled,
        rejects_count=rejects_count,
        fill_rate=fill_rate,
        avg_time_to_fill=None,
        slippage_bps_avg=slippage_bps_avg,
        per_symbol=per_symbol,
    )
