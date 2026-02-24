from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypeVar

from btcbot.persistence.sqlite.sqlite_connection import sqlite_connection_context

_REQUIRED_PARITY_TABLES = ("stage7_cycle_trace", "stage7_ledger_metrics")

_TJsonFallback = TypeVar("_TJsonFallback", dict[str, object], list[object])


def find_missing_stage7_parity_tables(db_path: str | Path) -> list[str]:
    with sqlite_connection_context(str(db_path)) as conn:
        existing = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    return [name for name in _REQUIRED_PARITY_TABLES if name not in existing]


def compute_run_fingerprint(
    db_path: str | Path,
    from_ts: datetime,
    to_ts: datetime,
    *,
    quantize_try: Decimal | None = None,
    include_adaptation: bool = False,
) -> str:
    start = _iso(from_ts)
    end = _iso(to_ts)
    missing = find_missing_stage7_parity_tables(db_path)
    if missing:
        payload = {
            "version": 1,
            "missing_tables": sorted(missing),
            "rows": [],
            "include_adaptation": include_adaptation,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    with sqlite_connection_context(str(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
              c.cycle_id,
              c.ts,
              c.selected_universe_json,
              c.intents_summary_json,
              c.mode_json,
              c.active_param_version,
              c.param_change_json,
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

    canonical: list[dict[str, object]] = []
    for row in rows:
        intents_summary = _safe_load_json(row["intents_summary_json"], fallback={})
        mode_payload = _safe_load_json(row["mode_json"], fallback={})
        universe_payload = _safe_load_json(row["selected_universe_json"], fallback=[])
        universe = sorted({str(item) for item in universe_payload})
        oms_summary_raw = intents_summary.get("oms_summary")
        oms_summary = dict(oms_summary_raw) if isinstance(oms_summary_raw, dict) else {}
        item: dict[str, object] = {
            "ts": row["ts"],
            "cycle_id": row["cycle_id"],
            "base_mode": mode_payload.get("base_mode"),
            "final_mode": mode_payload.get("final_mode"),
            "selected_universe": universe,
            "net_pnl_try": _format_try_metric(row["net_pnl_try"], quantize=quantize_try),
            "fees_try": _format_try_metric(row["fees_try"], quantize=quantize_try),
            "slippage_try": _format_try_metric(row["slippage_try"], quantize=quantize_try),
            "turnover_try": _format_try_metric(row["turnover_try"], quantize=quantize_try),
            "intents_count": int(str(intents_summary.get("order_intents_total", 0))),
            "filled_count": int(oms_summary.get("orders_filled", 0)),
            "rejected_count": int(oms_summary.get("orders_rejected", 0)),
        }
        if include_adaptation:
            item["active_param_version"] = row["active_param_version"]
            item["param_change"] = _safe_load_json(row["param_change_json"], fallback={})
        canonical.append(item)

    canonical_payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def compare_fingerprints(f1: str, f2: str) -> bool:
    return str(f1).strip() == str(f2).strip()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _safe_load_json(raw: object, *, fallback: _TJsonFallback) -> _TJsonFallback:
    text = str(raw or "").strip()
    if not text or text.lower() == "none":
        return fallback
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return fallback
    return loaded if isinstance(loaded, type(fallback)) else fallback


def _format_try_metric(value: object, *, quantize: Decimal | None) -> str:
    if quantize is None:
        return str(value)
    try:
        return str(Decimal(str(value)).quantize(quantize))
    except (InvalidOperation, ValueError):
        return str(value)
