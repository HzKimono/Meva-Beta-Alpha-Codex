from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from btcbot.domain.execution_quality import compute_execution_quality
from btcbot.domain.stage4 import Fill, PnLSnapshot
from btcbot.services.ledger_service import LedgerIngestResult, PnlReport
from btcbot.services.state_store import StateStore


@dataclass(frozen=True)
class CycleMetrics:
    cycle_id: str
    ts_start: datetime
    ts_end: datetime
    mode: str
    fills_count: int
    orders_submitted: int
    orders_canceled: int
    rejects_count: int
    fills_per_submitted_order: float
    avg_time_to_fill: float | None
    slippage_bps_avg: float | None
    fees: dict[str, str]
    pnl: dict[str, str]
    meta: dict[str, object]


def build_cycle_metrics(
    cycle_id: str,
    cycle_started_at: datetime,
    cycle_ended_at: datetime,
    *,
    mode: str,
    fills: list[Fill],
    ledger_append_result: LedgerIngestResult,
    pnl_report: PnlReport,
    orders_submitted: int,
    orders_canceled: int,
    rejects_count: int,
    mark_prices: dict[str, Decimal],
    pnl_snapshot: PnLSnapshot,
) -> CycleMetrics:
    quality = compute_execution_quality(
        {
            "orders_submitted": orders_submitted,
            "orders_canceled": orders_canceled,
            "rejects_count": rejects_count,
        },
        fills,
        mark_prices,
    )
    return CycleMetrics(
        cycle_id=cycle_id,
        ts_start=cycle_started_at,
        ts_end=cycle_ended_at,
        mode=mode,
        fills_count=quality.fills_count,
        orders_submitted=quality.orders_submitted,
        orders_canceled=quality.orders_canceled,
        rejects_count=quality.rejects_count,
        fills_per_submitted_order=float(quality.fills_per_submitted_order),
        avg_time_to_fill=quality.avg_time_to_fill,
        slippage_bps_avg=(float(quality.slippage_bps_avg) if quality.slippage_bps_avg else None),
        fees={k: str(v) for k, v in pnl_report.fees_total_by_currency.items()},
        pnl={
            "realized_pnl_total": str(pnl_report.realized_pnl_total),
            "unrealized_pnl_total": str(pnl_report.unrealized_pnl_total),
            "equity_estimate": str(pnl_report.equity_estimate),
            "realized_today_try": str(pnl_snapshot.realized_today_try),
        },
        meta={
            "fill_rate_semantics": "fills_per_submitted_order",
            "ledger_events_attempted": ledger_append_result.events_attempted,
            "ledger_events_inserted": ledger_append_result.events_inserted,
            "ledger_events_ignored": ledger_append_result.events_ignored,
            "per_symbol": [
                {
                    "symbol": item.symbol,
                    "fills_count": item.fills_count,
                    "slippage_bps_avg": (
                        float(item.slippage_bps_avg) if item.slippage_bps_avg is not None else None
                    ),
                    "fills_per_submitted_order": (
                        float(item.fills_per_submitted_order)
                        if item.fills_per_submitted_order is not None
                        else None
                    ),
                }
                for item in quality.per_symbol
            ],
        },
    )


def persist_cycle_metrics(state_store: StateStore, cycle_metrics: CycleMetrics) -> None:
    state_store.save_cycle_metrics(
        cycle_id=cycle_metrics.cycle_id,
        ts_start=cycle_metrics.ts_start.isoformat(),
        ts_end=cycle_metrics.ts_end.isoformat(),
        mode=cycle_metrics.mode,
        fills_count=cycle_metrics.fills_count,
        orders_submitted=cycle_metrics.orders_submitted,
        orders_canceled=cycle_metrics.orders_canceled,
        rejects_count=cycle_metrics.rejects_count,
        fill_rate=cycle_metrics.fills_per_submitted_order,
        avg_time_to_fill=cycle_metrics.avg_time_to_fill,
        slippage_bps_avg=cycle_metrics.slippage_bps_avg,
        fees_json=json.dumps(cycle_metrics.fees, sort_keys=True),
        pnl_json=json.dumps(cycle_metrics.pnl, sort_keys=True),
        meta_json=json.dumps(cycle_metrics.meta, sort_keys=True),
    )
