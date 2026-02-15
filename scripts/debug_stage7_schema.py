from __future__ import annotations

import json
import sqlite3
import sys


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "btcbot_state.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
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
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
