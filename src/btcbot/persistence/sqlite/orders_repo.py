from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import logging
import sqlite3

from btcbot.domain.models import normalize_symbol
from btcbot.domain.stage4 import Order as Stage4Order
from btcbot.persistence.interfaces.orders_repo import Stage4SubmitDedupeStatus
from btcbot.persistence.sqlite.sqlite_connection import ensure_stage4_schema

logger = logging.getLogger(__name__)


class SqliteOrdersRepo:
    def __init__(self, conn: sqlite3.Connection, *, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only
        ensure_stage4_schema(conn)

    def _ensure_writable(self) -> None:
        if self._read_only:
            logger.warning("read_only_write_blocked", extra={"extra": {"repo": "orders"}})
            raise PermissionError("UnitOfWork is read-only; orders writes are blocked")

    def _row_to_stage4_order(self, row: sqlite3.Row) -> Stage4Order:
        return Stage4Order(
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            type="limit",
            price=Decimal(str(row["price"])),
            qty=Decimal(str(row["qty"])),
            status=str(row["status"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            exchange_order_id=(str(row["exchange_order_id"]) if row["exchange_order_id"] else None),
            client_order_id=(str(row["client_order_id"]) if row["client_order_id"] else None),
            exchange_client_id=(
                str(row["exchange_client_id"]) if row["exchange_client_id"] else None
            ),
            mode=str(row["mode"]),
        )

    def client_order_id_exists(self, client_order_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM stage4_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return row is not None

    def stage4_has_unknown_orders(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM stage4_orders WHERE status = 'unknown' LIMIT 1"
        ).fetchone()
        return row is not None

    def stage4_unknown_client_order_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT client_order_id FROM stage4_orders WHERE status = 'unknown' ORDER BY updated_at DESC"
        ).fetchall()
        return [str(row["client_order_id"]) for row in rows if row["client_order_id"]]

    def get_stage4_order_by_client_id(self, client_order_id: str):
        row = self._conn.execute(
            "SELECT * FROM stage4_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_stage4_order(row)

    def list_stage4_open_orders(
        self,
        symbol: str | None = None,
        *,
        include_external: bool = False,
        include_unknown: bool = False,
    ):
        statuses = ["open", "submitted", "cancel_requested"]
        if include_unknown:
            statuses.append("unknown")
        status_clause = ",".join(f"'{status}'" for status in statuses)
        query = f"SELECT * FROM stage4_orders WHERE status IN ({status_clause})"
        if not include_external:
            query += " AND mode != 'external'"
        params: list[str] = []
        if symbol is not None:
            query += " AND symbol = ?"
            params.append(normalize_symbol(symbol))
        query += " ORDER BY symbol, side, created_at"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_stage4_order(row) for row in rows]

    def is_order_terminal(self, client_order_id: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM stage4_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return False
        return str(row["status"]).lower() in {"filled", "canceled", "rejected", "unknown_closed"}

    def stage4_submit_dedupe_status(
        self,
        *,
        internal_client_order_id: str,
        exchange_client_order_id: str,
    ) -> Stage4SubmitDedupeStatus:
        row = self._conn.execute(
            """
            SELECT id, status, created_at
            FROM stage4_orders
            WHERE client_order_id = ? OR exchange_client_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (internal_client_order_id, exchange_client_order_id),
        ).fetchone()
        if row is None:
            return Stage4SubmitDedupeStatus(
                should_dedupe=False,
                dedupe_key=exchange_client_order_id,
            )

        status = str(row["status"]).lower()
        created_at = datetime.fromisoformat(str(row["created_at"]))
        age_seconds = int((datetime.now(UTC) - created_at).total_seconds())
        reason: str | None = None
        if status == "open":
            reason = "open_order_exists"
        elif status in {"submitted", "cancel_requested"}:
            reason = "in_flight"
        elif status == "filled" and age_seconds < 5:
            reason = "recent_success"

        return Stage4SubmitDedupeStatus(
            should_dedupe=reason is not None,
            dedupe_key=exchange_client_order_id,
            reason=reason,
            age_seconds=age_seconds,
            related_order_id=str(row["id"]),
            related_status=status,
        )

    def record_stage4_order_submitted(
        self,
        *,
        symbol: str,
        client_order_id: str,
        exchange_client_id: str | None = None,
        exchange_order_id: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        mode: str,
        status: str = "open",
    ) -> None:
        self._ensure_writable()
        now = datetime.now(UTC).isoformat()
        existing = self._conn.execute(
            "SELECT id FROM stage4_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO stage4_orders(
                    symbol, client_order_id, exchange_client_id, exchange_order_id,
                    side, price, qty, status, mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalize_symbol(symbol),
                    client_order_id,
                    exchange_client_id,
                    exchange_order_id,
                    side,
                    str(price),
                    str(qty),
                    status,
                    mode,
                    now,
                    now,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE stage4_orders
                SET exchange_client_id=COALESCE(exchange_client_id, ?),
                    exchange_order_id=?, status=?, mode=?, updated_at=?
                WHERE client_order_id=?
                """,
                (
                    exchange_client_id,
                    exchange_order_id,
                    status,
                    mode,
                    now,
                    client_order_id,
                ),
            )

    def record_stage4_order_simulated_submit(
        self,
        *,
        symbol: str,
        client_order_id: str,
        side: str,
        price: Decimal,
        qty: Decimal,
    ) -> None:
        self.record_stage4_order_submitted(
            symbol=symbol,
            client_order_id=client_order_id,
            exchange_client_id=client_order_id,
            exchange_order_id=f"sim-{client_order_id}",
            side=side,
            price=price,
            qty=qty,
            mode="dry_run",
            status="open",
        )

    def record_stage4_order_cancel_requested(self, client_order_id: str) -> None:
        self._ensure_writable()
        self._conn.execute(
            """
            UPDATE stage4_orders
            SET status='cancel_requested', updated_at=?
            WHERE client_order_id=? AND status IN ('open','submitted')
            """,
            (datetime.now(UTC).isoformat(), client_order_id),
        )

    def record_stage4_order_canceled(self, client_order_id: str) -> None:
        self._ensure_writable()
        self._conn.execute(
            "UPDATE stage4_orders SET status='canceled', updated_at=? WHERE client_order_id=?",
            (datetime.now(UTC).isoformat(), client_order_id),
        )

    def record_stage4_order_error(
        self,
        *,
        client_order_id: str,
        reason: str,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        mode: str,
        status: str = "error",
    ) -> None:
        self._ensure_writable()
        now = datetime.now(UTC).isoformat()
        existing = self._conn.execute(
            "SELECT id FROM stage4_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO stage4_orders(
                    symbol, client_order_id, exchange_client_id, exchange_order_id,
                    side, price, qty, status, mode, last_error, created_at, updated_at
                ) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalize_symbol(symbol),
                    client_order_id,
                    side,
                    str(price),
                    str(qty),
                    status,
                    mode,
                    reason,
                    now,
                    now,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE stage4_orders
                SET symbol=?, side=?, price=?, qty=?,
                    status=?, mode=?, last_error=?, updated_at=?
                WHERE client_order_id=?
                """,
                (
                    normalize_symbol(symbol),
                    side,
                    str(price),
                    str(qty),
                    status,
                    mode,
                    reason,
                    now,
                    client_order_id,
                ),
            )

    def record_stage4_order_rejected(
        self,
        client_order_id: str,
        reason: str,
        *,
        symbol: str = "UNKNOWN",
        side: str = "unknown",
        price: Decimal = Decimal("0"),
        qty: Decimal = Decimal("0"),
        mode: str = "dry_run",
    ) -> None:
        self.record_stage4_order_error(
            client_order_id=client_order_id,
            reason=reason,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            mode=mode,
            status="rejected",
        )

    def update_stage4_order_exchange_id(self, client_order_id: str, exchange_order_id: str) -> None:
        self._ensure_writable()
        self._conn.execute(
            """
            UPDATE stage4_orders
            SET exchange_order_id=?, updated_at=?
            WHERE client_order_id=?
            """,
            (exchange_order_id, datetime.now(UTC).isoformat(), client_order_id),
        )

    def mark_stage4_unknown_closed(self, client_order_id: str) -> None:
        self._ensure_writable()
        self._conn.execute(
            """
            UPDATE stage4_orders
            SET status='unknown_closed', updated_at=?
            WHERE client_order_id=?
            """,
            (datetime.now(UTC).isoformat(), client_order_id),
        )

    def import_stage4_external_order(self, order: object) -> None:
        self._ensure_writable()
        client_order_id = getattr(order, "client_order_id", None)
        exchange_order_id = getattr(order, "exchange_order_id", None)
        if exchange_order_id is None:
            return
        now = datetime.now(UTC).isoformat()
        existing = None
        if client_order_id is not None:
            existing = self._conn.execute(
                "SELECT id, exchange_order_id FROM stage4_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO stage4_orders(
                    symbol, client_order_id, exchange_client_id, exchange_order_id,
                    side, price, qty, status, mode, created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'external', ?, ?)
                """,
                (
                    normalize_symbol(str(getattr(order, "symbol", "UNKNOWN"))),
                    client_order_id,
                    exchange_order_id,
                    str(getattr(order, "side", "unknown")),
                    str(getattr(order, "price", Decimal("0"))),
                    str(getattr(order, "qty", Decimal("0"))),
                    str(getattr(order, "status", "open")),
                    now,
                    now,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE stage4_orders
                SET exchange_order_id=COALESCE(exchange_order_id, ?), updated_at=?
                WHERE client_order_id=?
                """,
                (exchange_order_id, now, client_order_id),
            )

    def get_stage4_order_by_exchange_id(self, exchange_order_id: str):
        row = self._conn.execute(
            "SELECT * FROM stage4_orders WHERE exchange_order_id = ?",
            (exchange_order_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_stage4_order(row)
