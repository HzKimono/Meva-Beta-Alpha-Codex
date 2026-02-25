from __future__ import annotations

import json
import os
import sys
from typing import Any

from btcbot.persistence.sqlite.sqlite_connection import sqlite_connection_context


def table_columns(conn: Any, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else (os.getenv("STATE_DB_PATH") or "btcbot_state.db")
    with sqlite_connection_context(db_path) as conn:
        tables = [
            "stage7_run_metrics",
            "stage7_ledger_metrics",
            "stage7_risk_decisions",
            "cycle_metrics",
            "anomaly_events",
        ]
        payload = {table: table_columns(conn, table) for table in tables}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
