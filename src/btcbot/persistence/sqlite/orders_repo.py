from __future__ import annotations

import sqlite3


class SqliteOrdersRepo:
    def __init__(self, conn: sqlite3.Connection, *, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only

    def client_order_id_exists(self, client_order_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM stage4_orders WHERE client_order_id = ? LIMIT 1", (client_order_id,)
        ).fetchone()
        return row is not None
