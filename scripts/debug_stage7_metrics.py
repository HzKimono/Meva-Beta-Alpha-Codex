from __future__ import annotations

import json
import os
import sys

from btcbot.persistence.sqlite.sqlite_connection import sqlite_connection_context


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else (os.getenv("STATE_DB_PATH") or "btcbot_state.db")
    with sqlite_connection_context(db_path) as conn:
        row = conn.execute(
            """
            SELECT m.cycle_id, m.ts, m.mode_final, m.oms_rejected_count,
                   m.max_drawdown_ratio, m.max_drawdown_pct,
                   m.equity_try, m.net_pnl_try, m.turnover_try,
                   m.alert_flags_json, l.realized_pnl_try, l.unrealized_pnl_try,
                   l.fees_try, l.slippage_try, l.max_drawdown_ratio as ledger_drawdown_ratio
            FROM stage7_run_metrics m
            LEFT JOIN stage7_ledger_metrics l ON l.cycle_id = m.cycle_id
            ORDER BY m.ts DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            print(json.dumps({"status": "empty"}, indent=2))
            return 0
        payload = {k: row[k] for k in row.keys()}
        payload["alert_flags"] = json.loads(str(payload.pop("alert_flags_json") or "{}"))
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
