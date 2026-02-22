from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


class SqliteTraceRepo:
    def __init__(self, conn: sqlite3.Connection, *, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only

    def _ensure_writable(self) -> None:
        if self._read_only:
            logger.warning("read_only_write_blocked", extra={"extra": {"repo": "trace"}})
            raise PermissionError("UnitOfWork is read-only; trace writes are blocked")

    def record_cycle_audit(
        self,
        cycle_id: str,
        counts: dict[str, int],
        decisions: list[str],
        envelope: dict[str, object] | None = None,
    ) -> None:
        self._ensure_writable()
        self._conn.execute(
            """
            INSERT INTO cycle_audit(cycle_id, ts, counts_json, decisions_json, envelope_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id) DO UPDATE SET
                ts=excluded.ts,
                counts_json=excluded.counts_json,
                decisions_json=excluded.decisions_json,
                envelope_json=excluded.envelope_json
            """,
            (
                cycle_id,
                datetime.now(UTC).isoformat(),
                json.dumps(counts, sort_keys=True),
                json.dumps(decisions, sort_keys=True),
                (json.dumps(envelope, sort_keys=True) if envelope is not None else None),
            ),
        )
