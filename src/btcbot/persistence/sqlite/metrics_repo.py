from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


class SqliteMetricsRepo:
    def __init__(self, conn: sqlite3.Connection, *, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only

    def _ensure_writable(self) -> None:
        if self._read_only:
            logger.warning("read_only_write_blocked", extra={"extra": {"repo": "metrics"}})
            raise PermissionError("UnitOfWork is read-only; metrics writes are blocked")

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
    ) -> None:
        self._ensure_writable()
        self._conn.execute(
            """
            INSERT INTO cycle_metrics(
                cycle_id, ts_start, ts_end, mode, fills_count, orders_submitted,
                orders_canceled, rejects_count, fill_rate, avg_time_to_fill,
                slippage_bps_avg, fees_json, pnl_json, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id) DO UPDATE SET
                ts_start=excluded.ts_start,
                ts_end=excluded.ts_end,
                mode=excluded.mode,
                fills_count=excluded.fills_count,
                orders_submitted=excluded.orders_submitted,
                orders_canceled=excluded.orders_canceled,
                rejects_count=excluded.rejects_count,
                fill_rate=excluded.fill_rate,
                avg_time_to_fill=excluded.avg_time_to_fill,
                slippage_bps_avg=excluded.slippage_bps_avg,
                fees_json=excluded.fees_json,
                pnl_json=excluded.pnl_json,
                meta_json=excluded.meta_json
            """,
            (
                cycle_id,
                ts_start,
                ts_end,
                mode,
                fills_count,
                orders_submitted,
                orders_canceled,
                rejects_count,
                fill_rate,
                avg_time_to_fill,
                slippage_bps_avg,
                fees_json,
                pnl_json,
                meta_json,
            ),
        )
