from __future__ import annotations

import sqlite3


def create_sqlite_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_stage4_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            client_order_id TEXT,
            exchange_client_id TEXT,
            exchange_order_id TEXT,
            side TEXT NOT NULL,
            price TEXT NOT NULL,
            qty TEXT NOT NULL,
            status TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'dry_run',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stage4_orders_client_order_id_unique
        ON stage4_orders(client_order_id)
        WHERE client_order_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stage4_orders_exchange_client_id_unique
        ON stage4_orders(exchange_client_id)
        WHERE exchange_client_id IS NOT NULL
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stage4_orders_status ON stage4_orders(status)")
    order_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(stage4_orders)")}
    if "mode" not in order_columns:
        conn.execute("ALTER TABLE stage4_orders ADD COLUMN mode TEXT NOT NULL DEFAULT 'dry_run'")
    if "last_error" not in order_columns:
        conn.execute("ALTER TABLE stage4_orders ADD COLUMN last_error TEXT")
    if "exchange_order_id" not in order_columns:
        conn.execute("ALTER TABLE stage4_orders ADD COLUMN exchange_order_id TEXT")
    if "exchange_client_id" not in order_columns:
        conn.execute("ALTER TABLE stage4_orders ADD COLUMN exchange_client_id TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_fills (
            fill_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            price TEXT NOT NULL,
            qty TEXT NOT NULL,
            fee TEXT NOT NULL,
            fee_asset TEXT NOT NULL,
            ts TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_replace_transactions (
            replace_tx_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            old_client_order_ids_json TEXT NOT NULL,
            new_client_order_id TEXT NOT NULL,
            state TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stage4_replace_transactions_state
        ON stage4_replace_transactions(state)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_positions (
            symbol TEXT PRIMARY KEY,
            qty TEXT NOT NULL,
            avg_cost_try TEXT NOT NULL,
            realized_pnl_try TEXT NOT NULL,
            last_update_ts TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pnl_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_equity_try TEXT NOT NULL,
            realized_today_try TEXT NOT NULL,
            realized_total_try TEXT NOT NULL DEFAULT '0',
            drawdown_pct TEXT NOT NULL,
            ts TEXT NOT NULL
        )
        """
    )
    pnl_cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(pnl_snapshots)")}
    if "realized_total_try" not in pnl_cols:
        conn.execute("ALTER TABLE pnl_snapshots ADD COLUMN realized_total_try TEXT NOT NULL DEFAULT '0'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pnl_snapshots_ts ON pnl_snapshots(ts)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applied_fills (
            fill_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_min_schema(conn: sqlite3.Connection) -> None:
    ensure_stage4_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cycle_metrics (
            cycle_id TEXT PRIMARY KEY,
            ts_start TEXT NOT NULL,
            ts_end TEXT NOT NULL,
            mode TEXT NOT NULL,
            fills_count INTEGER NOT NULL,
            orders_submitted INTEGER NOT NULL,
            orders_canceled INTEGER NOT NULL,
            rejects_count INTEGER NOT NULL,
            fill_rate REAL NOT NULL,
            avg_time_to_fill REAL,
            slippage_bps_avg REAL,
            fees_json TEXT NOT NULL,
            pnl_json TEXT NOT NULL,
            meta_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cycle_audit (
            cycle_id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            counts_json TEXT NOT NULL,
            decisions_json TEXT NOT NULL,
            envelope_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_decisions (
            decision_id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            mode TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            signals_json TEXT NOT NULL,
            limits_json TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            prev_mode TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_state_current (
            state_id INTEGER PRIMARY KEY CHECK(state_id = 1),
            current_mode TEXT,
            peak_equity_try TEXT,
            peak_equity_date TEXT,
            fees_try_today TEXT,
            fees_day TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
