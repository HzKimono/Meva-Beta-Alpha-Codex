from __future__ import annotations

import sqlite3


def create_sqlite_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_min_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage4_orders (
            client_order_id TEXT PRIMARY KEY
        )
        """
    )
