from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.risk_budget import Mode, RiskDecision
from btcbot.domain.risk_mode_codec import dump_risk_mode
from btcbot.persistence.interfaces.risk_repo import serialize_risk_payload

logger = logging.getLogger(__name__)


class SqliteRiskRepo:
    def __init__(self, conn: sqlite3.Connection, *, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only

    def _ensure_writable(self) -> None:
        if self._read_only:
            logger.warning("read_only_write_blocked", extra={"extra": {"repo": "risk"}})
            raise PermissionError("UnitOfWork is read-only; risk writes are blocked")

    def get_risk_state_current(self) -> dict[str, str | None]:
        row = self._conn.execute("SELECT * FROM risk_state_current WHERE state_id = 1").fetchone()
        if row is None:
            return {
                "current_mode": None,
                "peak_equity_try": None,
                "peak_equity_date": None,
                "fees_try_today": None,
                "fees_day": None,
            }
        return {
            "current_mode": (str(row["current_mode"]) if row["current_mode"] is not None else None),
            "peak_equity_try": (
                str(row["peak_equity_try"]) if row["peak_equity_try"] is not None else None
            ),
            "peak_equity_date": (
                str(row["peak_equity_date"]) if row["peak_equity_date"] is not None else None
            ),
            "fees_try_today": (
                str(row["fees_try_today"]) if row["fees_try_today"] is not None else None
            ),
            "fees_day": str(row["fees_day"]) if row["fees_day"] is not None else None,
        }

    def save_risk_decision(
        self, *, cycle_id: str, decision: RiskDecision, prev_mode: Mode | None
    ) -> None:
        self._ensure_writable()
        self._conn.execute(
            """
            INSERT INTO risk_decisions(
                decision_id, ts, mode, reasons_json, signals_json, limits_json, decision_json, prev_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id) DO UPDATE SET
                ts=excluded.ts,
                mode=excluded.mode,
                reasons_json=excluded.reasons_json,
                signals_json=excluded.signals_json,
                limits_json=excluded.limits_json,
                decision_json=excluded.decision_json,
                prev_mode=excluded.prev_mode
            """,
            (
                cycle_id,
                decision.decided_at.isoformat(),
                dump_risk_mode(decision.mode),
                json.dumps(decision.reasons, sort_keys=True),
                serialize_risk_payload(decision.signals),
                serialize_risk_payload(decision.limits),
                serialize_risk_payload(decision),
                dump_risk_mode(prev_mode),
            ),
        )

    def upsert_risk_state_current(
        self,
        *,
        risk_mode: Mode,
        peak_equity_try: Decimal,
        peak_equity_date: str,
        fees_try_today: Decimal,
        fees_day: str,
    ) -> None:
        self._ensure_writable()
        self._conn.execute(
            """
            INSERT INTO risk_state_current(
                state_id, current_mode, peak_equity_try, peak_equity_date,
                fees_try_today, fees_day, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(state_id) DO UPDATE SET
                current_mode=excluded.current_mode,
                peak_equity_try=excluded.peak_equity_try,
                peak_equity_date=excluded.peak_equity_date,
                fees_try_today=excluded.fees_try_today,
                fees_day=excluded.fees_day,
                updated_at=excluded.updated_at
            """,
            (
                dump_risk_mode(risk_mode),
                str(peak_equity_try),
                peak_equity_date,
                str(fees_try_today),
                fees_day,
                datetime.now(UTC).isoformat(),
            ),
        )
