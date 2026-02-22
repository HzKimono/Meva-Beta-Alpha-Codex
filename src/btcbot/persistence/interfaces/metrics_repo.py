from __future__ import annotations

from typing import Protocol


class MetricsRepoProtocol(Protocol):
    def save_cycle_metrics(
        self,
        *,
        cycle_id: str,
        ts_start: str,
        ts_end: str,
        mode: str,
        fills_count: int,
        orders_submitted: int,
        orders_canceled: int,
        rejects_count: int,
        fill_rate: float,
        avg_time_to_fill: float | None,
        slippage_bps_avg: float | None,
        fees_json: str,
        pnl_json: str,
        meta_json: str,
    ) -> None: ...
