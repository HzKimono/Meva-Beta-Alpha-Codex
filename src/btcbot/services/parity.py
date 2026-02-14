from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


def compute_run_fingerprint(db_path: str | Path, from_ts: datetime, to_ts: datetime) -> str:
    start = _iso(from_ts)
    end = _iso(to_ts)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              c.cycle_id,
              c.ts,
              c.selected_universe_json,
              c.intents_summary_json,
              c.mode_json,
              l.net_pnl_try,
              l.fees_try,
              l.slippage_try,
              l.turnover_try
            FROM stage7_cycle_trace c
            JOIN stage7_ledger_metrics l ON l.cycle_id = c.cycle_id
            WHERE c.ts >= ? AND c.ts <= ?
            ORDER BY c.ts ASC, c.cycle_id ASC
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    canonical: list[dict[str, object]] = []
    for row in rows:
        intents_summary_raw = row["intents_summary_json"] or "{}"
        mode_payload_raw = row["mode_json"] or "{}"
        universe_raw = row["selected_universe_json"] or "[]"
        intents_summary = json.loads(str(intents_summary_raw))
        mode_payload = json.loads(str(mode_payload_raw))
        universe = sorted(set(json.loads(str(universe_raw))))
        oms_summary = dict(intents_summary.get("oms_summary") or {})
        canonical.append(
            {
                "ts": row["ts"],
                "cycle_id": row["cycle_id"],
                "base_mode": mode_payload.get("base_mode"),
                "final_mode": mode_payload.get("final_mode"),
                "selected_universe": universe,
                "net_pnl_try": _quantized_try(row["net_pnl_try"]),
                "fees_try": _quantized_try(row["fees_try"]),
                "slippage_try": _quantized_try(row["slippage_try"]),
                "turnover_try": _quantized_try(row["turnover_try"]),
                "intents_count": int(intents_summary.get("order_intents_total", 0)),
                "filled_count": int(oms_summary.get("orders_filled", 0)),
                "rejected_count": int(oms_summary.get("orders_rejected", 0)),
            }
        )

    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare_fingerprints(f1: str, f2: str) -> bool:
    return str(f1).strip() == str(f2).strip()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _quantized_try(value: object) -> str:
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return str(value)
