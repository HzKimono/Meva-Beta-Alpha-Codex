from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from btcbot.domain.account_snapshot import AccountSnapshot, Holding
from btcbot.domain.accounting import Position, TradeFill
from btcbot.domain.adaptation_models import ParamChange, Stage7Params
from btcbot.domain.intent import Intent
from btcbot.domain.ledger import LedgerEvent, LedgerEventType, ensure_utc
from btcbot.domain.models import Order, OrderStatus, normalize_symbol
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.order_state import OrderEvent, Stage7Order
from btcbot.domain.order_state import OrderStatus as Stage7OrderStatus
from btcbot.domain.stage4 import Fill as Stage4Fill
from btcbot.domain.stage4 import PnLSnapshot
from btcbot.domain.stage4 import Position as Stage4Position

if TYPE_CHECKING:
    from btcbot.domain.anomalies import AnomalyEvent
    from btcbot.domain.risk_budget import Mode, RiskDecision
    from btcbot.domain.risk_models import RiskDecision as Stage7RiskDecision


def _stage7_ctx(cycle_id: str, run_id: str | None = None) -> str:
    if run_id:
        return f"cycle_id={cycle_id} run_id={run_id}"
    return f"cycle_id={cycle_id}"


@dataclass
class StoredOrder:
    order_id: str
    symbol: str
    client_order_id: str | None
    side: str
    price: Decimal
    quantity: Decimal
    status: OrderStatus
    last_seen_at: int | None
    reconciled: bool
    exchange_status_raw: str | None
    unknown_first_seen_at: int | None = None
    unknown_last_probe_at: int | None = None
    unknown_next_probe_at: int | None = None
    unknown_probe_attempts: int = 0
    unknown_escalated_at: int | None = None


@dataclass
class StoredIntentTs:
    symbol: str
    side: str
    created_at: datetime


@dataclass(frozen=True)
class AppendResult:
    attempted: int
    inserted: int
    ignored: int


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is re-used with a conflicting payload."""


PENDING_GRACE_SECONDS = 60


@dataclass(frozen=True)
class SubmitDedupeDecision:
    should_dedupe: bool
    dedupe_key: str
    reason: str | None = None
    age_seconds: int | None = None
    related_order_id: str | None = None
    related_status: str | None = None


@dataclass(frozen=True)
class ReservationResult:
    reserved: bool
    action_type: str
    key: str
    payload_hash: str
    created_at_epoch: int
    expires_at_epoch: int
    action_id: int | None
    client_order_id: str | None
    order_id: str | None
    status: str
    recovery_attempts: int
    next_recovery_at_epoch: int | None


class StateStore:
    def __init__(self, db_path: str = "btcbot_state.db") -> None:
        self.db_path = db_path
        self._transaction_conn: sqlite3.Connection | None = None
        self._init_db()
        with self._connect() as conn:
            self._ensure_risk_budget_schema(conn)
            self._ensure_anomaly_schema(conn)
            self._ensure_stage7_schema(conn)
            self._ensure_agent_audit_schema(conn)
            self._ensure_idempotency_schema(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        tx_conn = getattr(self, "_transaction_conn", None)
        if tx_conn is not None:
            yield tx_conn
            return
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        tx_conn = getattr(self, "_transaction_conn", None)
        if tx_conn is not None:
            yield tx_conn
            return
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        self._transaction_conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._transaction_conn = None
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    dedupe_key TEXT,
                    created_at_epoch INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_actions_type_hash_created
                ON actions(action_type, payload_hash, created_at_epoch)
                """
            )
            self._ensure_actions_metadata_columns(conn)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_dedupe_key_unique
                ON actions(dedupe_key)
                WHERE dedupe_key IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_orders_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_currency TEXT NOT NULL,
                    ts TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    qty TEXT NOT NULL,
                    avg_cost TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    unrealized_pnl TEXT NOT NULL,
                    fees_paid TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_idempotency_key
                ON intents(idempotency_key)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_stage4_schema(conn)
            self._ensure_ledger_schema(conn)
            self._ensure_cycle_metrics_schema(conn)
            self._ensure_risk_budget_schema(conn)
            self._ensure_anomaly_schema(conn)
            self._ensure_stage7_schema(conn)
            self._ensure_agent_audit_schema(conn)
            self._ensure_idempotency_schema(conn)

    def _ensure_agent_audit_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_decision_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                context_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                safe_decision_json TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                diff_hash TEXT NOT NULL,
                prompt_json TEXT,
                response_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_decision_audit_cycle
            ON agent_decision_audit(cycle_id, ts)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_decision_audit_cycle_correlation
            ON agent_decision_audit(cycle_id, correlation_id)
            """
        )

    def _ensure_risk_budget_schema(self, conn: sqlite3.Connection) -> None:
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
            "CREATE INDEX IF NOT EXISTS idx_risk_decisions_ts ON risk_decisions(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_risk_decisions_mode ON risk_decisions(mode)"
        )
        if not self._table_exists(conn, "risk_state_current"):
            msg = "risk_state_current not created"
            raise RuntimeError(msg)

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _ensure_anomaly_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anomaly_events (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_anomaly_events_ts ON anomaly_events(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_anomaly_events_code ON anomaly_events(code)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_anomaly_events_cycle_id ON anomaly_events(cycle_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS degrade_state_current (
                state_id INTEGER PRIMARY KEY CHECK(state_id = 1),
                cooldown_until TEXT,
                current_override_mode TEXT,
                last_reasons_json TEXT,
                warn_window_count INTEGER NOT NULL DEFAULT 0,
                last_warn_codes_json TEXT NOT NULL DEFAULT '[]',
                cursor_stall_cycles_json TEXT NOT NULL DEFAULT '{}',
                last_reject_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(degrade_state_current)")
        }
        if "warn_window_count" not in columns:
            conn.execute(
                "ALTER TABLE degrade_state_current "
                "ADD COLUMN warn_window_count INTEGER NOT NULL DEFAULT 0"
            )
        if "last_warn_codes_json" not in columns:
            conn.execute(
                "ALTER TABLE degrade_state_current "
                "ADD COLUMN last_warn_codes_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "cursor_stall_cycles_json" not in columns:
            conn.execute(
                "ALTER TABLE degrade_state_current "
                "ADD COLUMN cursor_stall_cycles_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "last_reject_count" not in columns:
            conn.execute(
                "ALTER TABLE degrade_state_current "
                "ADD COLUMN last_reject_count INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_stage7_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_cycle_trace (
                cycle_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                selected_universe_json TEXT NOT NULL,
                universe_scores_json TEXT NOT NULL DEFAULT '[]',
                intents_summary_json TEXT NOT NULL,
                mode_json TEXT NOT NULL,
                order_decisions_json TEXT NOT NULL,
                portfolio_plan_json TEXT NOT NULL DEFAULT '{}',
                order_intents_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(stage7_cycle_trace)")
        }
        if "universe_scores_json" not in columns:
            conn.execute(
                "ALTER TABLE stage7_cycle_trace "
                "ADD COLUMN universe_scores_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "portfolio_plan_json" not in columns:
            conn.execute(
                "ALTER TABLE stage7_cycle_trace "
                "ADD COLUMN portfolio_plan_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "order_intents_json" not in columns:
            conn.execute(
                "ALTER TABLE stage7_cycle_trace "
                "ADD COLUMN order_intents_json TEXT NOT NULL DEFAULT '[]'"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_ledger_metrics (
                cycle_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                gross_pnl_try TEXT NOT NULL,
                realized_pnl_try TEXT NOT NULL,
                unrealized_pnl_try TEXT NOT NULL,
                net_pnl_try TEXT NOT NULL,
                fees_try TEXT NOT NULL,
                slippage_try TEXT NOT NULL,
                turnover_try TEXT NOT NULL,
                equity_try TEXT NOT NULL,
                max_drawdown TEXT NOT NULL,
                max_drawdown_ratio TEXT NOT NULL DEFAULT "0",
                FOREIGN KEY(cycle_id) REFERENCES stage7_cycle_trace(cycle_id)
            )
            """
        )
        ledger_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(stage7_ledger_metrics)")
        }
        if "max_drawdown_ratio" not in ledger_columns:
            conn.execute(
                "ALTER TABLE stage7_ledger_metrics "
                "ADD COLUMN max_drawdown_ratio TEXT NOT NULL DEFAULT '0'"
            )
            conn.execute(
                "UPDATE stage7_ledger_metrics SET max_drawdown_ratio=max_drawdown "
                "WHERE max_drawdown_ratio='0'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_cycle_trace_ts ON stage7_cycle_trace(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_ledger_metrics_ts ON stage7_ledger_metrics(ts)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_run_metrics (
                cycle_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                mode_base TEXT NOT NULL,
                mode_final TEXT NOT NULL,
                universe_size INTEGER NOT NULL,
                intents_planned_count INTEGER NOT NULL,
                intents_skipped_count INTEGER NOT NULL,
                oms_submitted_count INTEGER NOT NULL,
                oms_filled_count INTEGER NOT NULL,
                oms_rejected_count INTEGER NOT NULL,
                oms_canceled_count INTEGER NOT NULL,
                fills_written_count INTEGER NOT NULL DEFAULT 0,
                fills_applied_count INTEGER NOT NULL DEFAULT 0,
                ledger_events_inserted INTEGER NOT NULL DEFAULT 0,
                positions_updated_count INTEGER NOT NULL DEFAULT 0,
                events_appended INTEGER NOT NULL,
                events_ignored INTEGER NOT NULL,
                equity_try TEXT NOT NULL,
                gross_pnl_try TEXT NOT NULL,
                net_pnl_try TEXT NOT NULL,
                fees_try TEXT NOT NULL,
                slippage_try TEXT NOT NULL,
                max_drawdown_pct TEXT NOT NULL,
                max_drawdown_ratio TEXT NOT NULL DEFAULT "0",
                turnover_try TEXT NOT NULL,
                latency_ms_total INTEGER NOT NULL,
                selection_ms INTEGER NOT NULL,
                planning_ms INTEGER NOT NULL,
                intents_ms INTEGER NOT NULL,
                oms_ms INTEGER NOT NULL,
                ledger_ms INTEGER NOT NULL,
                persist_ms INTEGER NOT NULL,
                quality_flags_json TEXT NOT NULL,
                alert_flags_json TEXT NOT NULL,
                no_trades_reason TEXT,
                no_metrics_reason TEXT,
                run_id TEXT
            )
            """
        )
        run_metric_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(stage7_run_metrics)")
        }
        if "fills_written_count" not in run_metric_columns:
            conn.execute(
                "ALTER TABLE stage7_run_metrics "
                "ADD COLUMN fills_written_count INTEGER NOT NULL DEFAULT 0"
            )
        if "fills_applied_count" not in run_metric_columns:
            conn.execute(
                "ALTER TABLE stage7_run_metrics "
                "ADD COLUMN fills_applied_count INTEGER NOT NULL DEFAULT 0"
            )
        if "ledger_events_inserted" not in run_metric_columns:
            conn.execute(
                "ALTER TABLE stage7_run_metrics "
                "ADD COLUMN ledger_events_inserted INTEGER NOT NULL DEFAULT 0"
            )
        if "positions_updated_count" not in run_metric_columns:
            conn.execute(
                "ALTER TABLE stage7_run_metrics "
                "ADD COLUMN positions_updated_count INTEGER NOT NULL DEFAULT 0"
            )
        if "max_drawdown_ratio" not in run_metric_columns:
            conn.execute(
                "ALTER TABLE stage7_run_metrics "
                "ADD COLUMN max_drawdown_ratio TEXT NOT NULL DEFAULT '0'"
            )
            conn.execute(
                "UPDATE stage7_run_metrics SET max_drawdown_ratio=max_drawdown_pct "
                "WHERE max_drawdown_ratio='0'"
            )
        if "run_id" not in run_metric_columns:
            conn.execute("ALTER TABLE stage7_run_metrics ADD COLUMN run_id TEXT")
        if "no_trades_reason" not in run_metric_columns:
            conn.execute(
                "ALTER TABLE stage7_run_metrics ADD COLUMN no_trades_reason TEXT"
            )
        if "no_metrics_reason" not in run_metric_columns:
            conn.execute(
                "ALTER TABLE stage7_run_metrics ADD COLUMN no_metrics_reason TEXT"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_run_metrics_ts ON stage7_run_metrics(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_run_metrics_run_id ON stage7_run_metrics(run_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_order_intents (
                client_order_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price_try TEXT NOT NULL,
                qty TEXT NOT NULL,
                notional_try TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PLANNED',
                intent_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_order_intents_cycle_id "
            "ON stage7_order_intents(cycle_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT UNIQUE NOT NULL,
                cycle_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price_try TEXT NOT NULL,
                qty TEXT NOT NULL,
                filled_qty TEXT NOT NULL,
                avg_fill_price_try TEXT,
                status TEXT NOT NULL,
                intent_hash TEXT NOT NULL,
                last_update TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_orders_client_order_id "
            "ON stage7_orders(client_order_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_order_events (
                event_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_order_events_client_ts "
            "ON stage7_order_events(client_order_id, ts)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_idempotency_keys (
                key TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_risk_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT,
                decided_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                cooldown_until TEXT,
                inputs_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage7_risk_decisions_decided_at "
            "ON stage7_risk_decisions(decided_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_params_active(
                key TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                params_json TEXT NOT NULL,
                ts TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_param_changes(
                change_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                from_version INTEGER NOT NULL,
                to_version INTEGER NOT NULL,
                change_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage7_params_checkpoints(
                version INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                params_json TEXT NOT NULL,
                is_good INTEGER NOT NULL
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(stage7_cycle_trace)")
        }
        if "active_param_version" not in columns:
            conn.execute(
                "ALTER TABLE stage7_cycle_trace "
                "ADD COLUMN active_param_version INTEGER NOT NULL DEFAULT 0"
            )
        if "param_change_json" not in columns:
            conn.execute(
                "ALTER TABLE stage7_cycle_trace "
                "ADD COLUMN param_change_json TEXT NOT NULL DEFAULT '{}'"
            )

    def save_stage7_run_metrics(
        self, cycle_id: str, metrics_dict: dict[str, object]
    ) -> None:
        with self._connect() as conn:
            self._save_stage7_run_metrics_with_conn(
                conn=conn, cycle_id=cycle_id, metrics_dict=metrics_dict
            )

    def _save_stage7_run_metrics_with_conn(
        self,
        *,
        conn: sqlite3.Connection,
        cycle_id: str,
        metrics_dict: dict[str, object],
    ) -> None:
        conn.execute(
            """
            INSERT INTO stage7_run_metrics(
                cycle_id, ts, mode_base, mode_final, universe_size,
                intents_planned_count, intents_skipped_count,
                oms_submitted_count, oms_filled_count, oms_rejected_count, oms_canceled_count,
                fills_written_count, fills_applied_count,
                ledger_events_inserted, positions_updated_count,
                events_appended, events_ignored,
                equity_try, gross_pnl_try, net_pnl_try, fees_try, slippage_try,
                max_drawdown_pct, max_drawdown_ratio, turnover_try,
                latency_ms_total, selection_ms, planning_ms, intents_ms,
                oms_ms, ledger_ms, persist_ms,
                quality_flags_json, alert_flags_json, no_trades_reason, no_metrics_reason, run_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(cycle_id) DO UPDATE SET
                ts=excluded.ts,
                mode_base=excluded.mode_base,
                mode_final=excluded.mode_final,
                universe_size=excluded.universe_size,
                intents_planned_count=excluded.intents_planned_count,
                intents_skipped_count=excluded.intents_skipped_count,
                oms_submitted_count=excluded.oms_submitted_count,
                oms_filled_count=excluded.oms_filled_count,
                oms_rejected_count=excluded.oms_rejected_count,
                oms_canceled_count=excluded.oms_canceled_count,
                fills_written_count=excluded.fills_written_count,
                fills_applied_count=excluded.fills_applied_count,
                ledger_events_inserted=excluded.ledger_events_inserted,
                positions_updated_count=excluded.positions_updated_count,
                events_appended=excluded.events_appended,
                events_ignored=excluded.events_ignored,
                equity_try=excluded.equity_try,
                gross_pnl_try=excluded.gross_pnl_try,
                net_pnl_try=excluded.net_pnl_try,
                fees_try=excluded.fees_try,
                slippage_try=excluded.slippage_try,
                max_drawdown_pct=excluded.max_drawdown_pct,
                max_drawdown_ratio=excluded.max_drawdown_ratio,
                turnover_try=excluded.turnover_try,
                latency_ms_total=excluded.latency_ms_total,
                selection_ms=excluded.selection_ms,
                planning_ms=excluded.planning_ms,
                intents_ms=excluded.intents_ms,
                oms_ms=excluded.oms_ms,
                ledger_ms=excluded.ledger_ms,
                persist_ms=excluded.persist_ms,
                quality_flags_json=excluded.quality_flags_json,
                alert_flags_json=excluded.alert_flags_json,
                no_trades_reason=excluded.no_trades_reason,
                no_metrics_reason=excluded.no_metrics_reason,
                run_id=excluded.run_id
            """,
            (
                cycle_id,
                str(metrics_dict["ts"]),
                str(metrics_dict["mode_base"]),
                str(metrics_dict["mode_final"]),
                int(metrics_dict["universe_size"]),
                int(metrics_dict["intents_planned_count"]),
                int(metrics_dict["intents_skipped_count"]),
                int(metrics_dict["oms_submitted_count"]),
                int(metrics_dict["oms_filled_count"]),
                int(metrics_dict["oms_rejected_count"]),
                int(metrics_dict["oms_canceled_count"]),
                int(metrics_dict.get("fills_written_count", 0)),
                int(metrics_dict.get("fills_applied_count", 0)),
                int(metrics_dict.get("ledger_events_inserted", 0)),
                int(metrics_dict.get("positions_updated_count", 0)),
                int(metrics_dict["events_appended"]),
                int(metrics_dict["events_ignored"]),
                str(metrics_dict["equity_try"]),
                str(metrics_dict["gross_pnl_try"]),
                str(metrics_dict["net_pnl_try"]),
                str(metrics_dict["fees_try"]),
                str(metrics_dict["slippage_try"]),
                str(metrics_dict["max_drawdown_pct"]),
                str(
                    metrics_dict.get(
                        "max_drawdown_ratio", metrics_dict["max_drawdown_pct"]
                    )
                ),
                str(metrics_dict["turnover_try"]),
                int(metrics_dict["latency_ms_total"]),
                int(metrics_dict["selection_ms"]),
                int(metrics_dict["planning_ms"]),
                int(metrics_dict["intents_ms"]),
                int(metrics_dict["oms_ms"]),
                int(metrics_dict["ledger_ms"]),
                int(metrics_dict["persist_ms"]),
                json.dumps(metrics_dict["quality_flags"], sort_keys=True),
                json.dumps(metrics_dict["alert_flags"], sort_keys=True),
                (
                    str(metrics_dict.get("no_trades_reason"))
                    if metrics_dict.get("no_trades_reason") not in (None, "")
                    else None
                ),
                (
                    str(metrics_dict.get("no_metrics_reason"))
                    if metrics_dict.get("no_metrics_reason") not in (None, "")
                    else None
                ),
                (
                    str(metrics_dict["run_id"])
                    if metrics_dict.get("run_id") not in (None, "")
                    else None
                ),
            ),
        )

    def fetch_stage7_run_metrics(
        self, limit: int, order_desc: bool = True
    ) -> list[dict[str, object]]:
        order_sql = "DESC" if order_desc else "ASC"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM stage7_run_metrics
                ORDER BY ts {order_sql}
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        payload: list[dict[str, object]] = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["quality_flags"] = json.loads(str(item.pop("quality_flags_json")))
            item["alert_flags"] = json.loads(str(item.pop("alert_flags_json")))
            payload.append(item)
        return payload

    def fetch_stage7_cycles_for_export(self, limit: int) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, c.intents_summary_json, c.mode_json
                FROM stage7_run_metrics m
                LEFT JOIN stage7_cycle_trace c ON c.cycle_id = m.cycle_id
                ORDER BY m.ts DESC
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        exports: list[dict[str, object]] = []
        for row in rows:
            record = {key: row[key] for key in row.keys()}
            record["quality_flags"] = json.loads(str(record.pop("quality_flags_json")))
            record["alert_flags"] = json.loads(str(record.pop("alert_flags_json")))
            record["intents_summary"] = json.loads(
                str(record.get("intents_summary_json") or "{}")
            )
            record["mode_payload"] = json.loads(str(record.get("mode_json") or "{}"))
            exports.append(record)
        return exports

    def get_stage7_cycle_trace(self, cycle_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stage7_cycle_trace WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
        if row is None:
            return None
        payload = {key: row[key] for key in row.keys()}
        payload["selected_universe"] = json.loads(
            str(payload.pop("selected_universe_json") or "[]")
        )
        payload["universe_scores"] = json.loads(
            str(payload.pop("universe_scores_json") or "[]")
        )
        payload["intents_summary"] = json.loads(
            str(payload.pop("intents_summary_json") or "{}")
        )
        payload["mode_payload"] = json.loads(str(payload.pop("mode_json") or "{}"))
        payload["order_decisions"] = json.loads(
            str(payload.pop("order_decisions_json") or "[]")
        )
        payload["portfolio_plan"] = json.loads(
            str(payload.pop("portfolio_plan_json") or "{}")
        )
        payload["order_intents"] = json.loads(
            str(payload.pop("order_intents_json") or "[]")
        )
        return payload

    def save_stage7_cycle(
        self,
        *,
        cycle_id: str,
        ts: datetime,
        selected_universe: list[str],
        universe_scores: list[dict[str, object]],
        intents_summary: dict[str, object],
        mode_payload: dict[str, object],
        order_decisions: list[dict[str, object]],
        portfolio_plan: dict[str, object],
        ledger_metrics: dict[str, Decimal],
        order_intents: list[OrderIntent] | None = None,
        order_intents_trace: list[dict[str, object]] | None = None,
        risk_decision: Stage7RiskDecision | None = None,
        run_metrics: dict[str, object] | None = None,
        active_param_version: int = 0,
        param_change: ParamChange | None = None,
    ) -> None:
        run_id = None
        if run_metrics is not None and run_metrics.get("run_id"):
            run_id = str(run_metrics.get("run_id"))

        try:
            with self.transaction() as conn:
                derived_trace = (
                    [
                        {
                            "client_order_id": intent.client_order_id,
                            "symbol": intent.symbol,
                            "side": intent.side,
                            "skipped": intent.skipped,
                            "skip_reason": intent.skip_reason,
                        }
                        for intent in order_intents
                    ]
                    if order_intents is not None
                    else []
                )
                if order_intents is not None and order_intents_trace is not None:
                    domain_ids = {item["client_order_id"] for item in derived_trace}
                    trace_ids = {
                        str(item.get("client_order_id"))
                        for item in order_intents_trace
                        if item.get("client_order_id")
                    }
                    if domain_ids != trace_ids:
                        msg = (
                            "save_stage7_cycle failed at intents_trace_validate "
                            f"{_stage7_ctx(cycle_id, run_id)}"
                        )
                        raise RuntimeError(msg)
                trace_payload = (
                    order_intents_trace
                    if order_intents_trace is not None
                    else derived_trace
                )
                try:
                    conn.execute(
                        """
                        INSERT INTO stage7_cycle_trace(
                            cycle_id, ts, selected_universe_json,
                            universe_scores_json, intents_summary_json,
                            mode_json, order_decisions_json,
                            portfolio_plan_json, order_intents_json,
                            active_param_version, param_change_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(cycle_id) DO UPDATE SET
                            ts=excluded.ts,
                            selected_universe_json=excluded.selected_universe_json,
                            universe_scores_json=excluded.universe_scores_json,
                            intents_summary_json=excluded.intents_summary_json,
                            mode_json=excluded.mode_json,
                            order_decisions_json=excluded.order_decisions_json,
                            portfolio_plan_json=excluded.portfolio_plan_json,
                            order_intents_json=excluded.order_intents_json,
                            active_param_version=excluded.active_param_version,
                            param_change_json=excluded.param_change_json
                        """,
                        (
                            cycle_id,
                            ensure_utc(ts).isoformat(),
                            json.dumps(selected_universe, sort_keys=True),
                            json.dumps(universe_scores, sort_keys=True),
                            json.dumps(intents_summary, sort_keys=True),
                            json.dumps(mode_payload, sort_keys=True),
                            json.dumps(order_decisions, sort_keys=True),
                            json.dumps(portfolio_plan, sort_keys=True),
                            json.dumps(trace_payload, sort_keys=True),
                            int(active_param_version),
                            (
                                json.dumps(param_change.to_dict(), sort_keys=True)
                                if param_change
                                else "{}"
                            ),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"save_stage7_cycle failed at cycle_trace_upsert "
                        f"{_stage7_ctx(cycle_id, run_id)}"
                    ) from exc

                if order_intents:
                    try:
                        self._save_stage7_order_intents(
                            conn=conn,
                            cycle_id=cycle_id,
                            ts=ts,
                            intents=order_intents,
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(
                            f"save_stage7_cycle failed at order_intents_upsert "
                            f"{_stage7_ctx(cycle_id, run_id)}"
                        ) from exc

                if risk_decision is not None:
                    try:
                        self._save_stage7_risk_decision_with_conn(
                            conn=conn,
                            cycle_id=cycle_id,
                            decision=risk_decision,
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(
                            f"save_stage7_cycle failed at risk_decision_insert "
                            f"{_stage7_ctx(cycle_id, run_id)}"
                        ) from exc

                if run_metrics is not None:
                    try:
                        self._save_stage7_run_metrics_with_conn(
                            conn=conn,
                            cycle_id=cycle_id,
                            metrics_dict=run_metrics,
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(
                            f"save_stage7_cycle failed at run_metrics_upsert "
                            f"{_stage7_ctx(cycle_id, run_id)}"
                        ) from exc

                try:
                    conn.execute(
                        """
                        INSERT INTO stage7_ledger_metrics(
                            cycle_id, ts, gross_pnl_try, realized_pnl_try, unrealized_pnl_try,
                            net_pnl_try, fees_try, slippage_try,
                            turnover_try, equity_try, max_drawdown, max_drawdown_ratio
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(cycle_id) DO UPDATE SET
                            ts=excluded.ts,
                            gross_pnl_try=excluded.gross_pnl_try,
                            realized_pnl_try=excluded.realized_pnl_try,
                            unrealized_pnl_try=excluded.unrealized_pnl_try,
                            net_pnl_try=excluded.net_pnl_try,
                            fees_try=excluded.fees_try,
                            slippage_try=excluded.slippage_try,
                            turnover_try=excluded.turnover_try,
                            equity_try=excluded.equity_try,
                            max_drawdown=excluded.max_drawdown,
                            max_drawdown_ratio=excluded.max_drawdown_ratio
                        """,
                        (
                            cycle_id,
                            ensure_utc(ts).isoformat(),
                            str(ledger_metrics["gross_pnl_try"]),
                            str(ledger_metrics["realized_pnl_try"]),
                            str(ledger_metrics["unrealized_pnl_try"]),
                            str(ledger_metrics["net_pnl_try"]),
                            str(ledger_metrics["fees_try"]),
                            str(ledger_metrics["slippage_try"]),
                            str(ledger_metrics["turnover_try"]),
                            str(ledger_metrics["equity_try"]),
                            str(ledger_metrics["max_drawdown"]),
                            str(
                                ledger_metrics.get(
                                    "max_drawdown_ratio", ledger_metrics["max_drawdown"]
                                )
                            ),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"save_stage7_cycle failed at ledger_metrics_upsert "
                        f"{_stage7_ctx(cycle_id, run_id)}"
                    ) from exc
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"save_stage7_cycle failed at transaction {_stage7_ctx(cycle_id, run_id)}"
            ) from exc

    def save_stage7_risk_decision(
        self,
        *,
        cycle_id: str | None,
        decision: Stage7RiskDecision,
    ) -> None:
        with self._connect() as conn:
            self._save_stage7_risk_decision_with_conn(
                conn=conn,
                cycle_id=cycle_id,
                decision=decision,
            )

    def _save_stage7_risk_decision_with_conn(
        self,
        *,
        conn: sqlite3.Connection,
        cycle_id: str | None,
        decision: Stage7RiskDecision,
    ) -> None:
        conn.execute(
            """
            INSERT INTO stage7_risk_decisions(
                cycle_id, decided_at, mode, reasons_json, cooldown_until, inputs_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                ensure_utc(decision.decided_at).isoformat(),
                decision.mode.value,
                json.dumps(decision.reasons, sort_keys=True),
                (
                    ensure_utc(decision.cooldown_until).isoformat()
                    if decision.cooldown_until is not None
                    else None
                ),
                decision.inputs_hash,
            ),
        )

    def get_latest_stage7_risk_decision(self) -> Stage7RiskDecision | None:
        from btcbot.domain.risk_models import RiskDecision as Stage7RiskDecision
        from btcbot.domain.risk_models import RiskMode

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT mode, reasons_json, cooldown_until, decided_at, inputs_hash
                FROM stage7_risk_decisions
                ORDER BY decided_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        cooldown = (
            datetime.fromisoformat(str(row["cooldown_until"]))
            if row["cooldown_until"] is not None
            else None
        )
        return Stage7RiskDecision(
            mode=RiskMode(str(row["mode"])),
            reasons=json.loads(str(row["reasons_json"])),
            cooldown_until=cooldown,
            decided_at=datetime.fromisoformat(str(row["decided_at"])),
            inputs_hash=str(row["inputs_hash"]),
        )

    def get_latest_stage7_ledger_metrics(self) -> dict[str, Decimal] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(max_drawdown_ratio, max_drawdown) AS max_drawdown_ratio,
                    net_pnl_try,
                    equity_try
                FROM stage7_ledger_metrics
                ORDER BY ts DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "max_drawdown_ratio": Decimal(str(row["max_drawdown_ratio"])),
            "net_pnl_try": Decimal(str(row["net_pnl_try"])),
            "equity_try": Decimal(str(row["equity_try"])),
        }

    def _save_stage7_order_intents(
        self,
        *,
        conn: sqlite3.Connection,
        cycle_id: str,
        ts: datetime,
        intents: list[OrderIntent],
    ) -> None:
        for intent in intents:
            conn.execute(
                """
                INSERT INTO stage7_order_intents(
                    client_order_id, cycle_id, ts, symbol, side,
                    order_type, price_try, qty, notional_try, status, intent_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    cycle_id=excluded.cycle_id,
                    ts=excluded.ts,
                    symbol=excluded.symbol,
                    side=excluded.side,
                    order_type=excluded.order_type,
                    price_try=excluded.price_try,
                    qty=excluded.qty,
                    notional_try=excluded.notional_try,
                    status=excluded.status,
                    intent_json=excluded.intent_json
                """,
                (
                    intent.client_order_id,
                    cycle_id,
                    ensure_utc(ts).isoformat(),
                    intent.symbol,
                    intent.side,
                    intent.order_type,
                    str(intent.price_try),
                    str(intent.qty),
                    str(intent.notional_try),
                    "SKIPPED" if intent.skipped else "PLANNED",
                    json.dumps(intent.to_dict(), sort_keys=True),
                ),
            )

    def save_stage7_order_intents(
        self, cycle_id: str, intents: list[OrderIntent]
    ) -> None:
        now = datetime.now(UTC)
        with self.transaction() as conn:
            self._save_stage7_order_intents(
                conn=conn, cycle_id=cycle_id, ts=now, intents=intents
            )

    def upsert_stage7_orders(self, orders: list[Stage7Order]) -> None:
        if not orders:
            return
        with self.transaction() as conn:
            for order in orders:
                conn.execute(
                    """
                    INSERT INTO stage7_orders(
                        order_id, client_order_id, cycle_id, symbol, side, order_type,
                        price_try, qty, filled_qty, avg_fill_price_try,
                        status, intent_hash, last_update
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(client_order_id) DO UPDATE SET
                        cycle_id=excluded.cycle_id,
                        symbol=excluded.symbol,
                        side=excluded.side,
                        order_type=excluded.order_type,
                        price_try=excluded.price_try,
                        qty=excluded.qty,
                        filled_qty=excluded.filled_qty,
                        avg_fill_price_try=excluded.avg_fill_price_try,
                        status=excluded.status,
                        intent_hash=excluded.intent_hash,
                        last_update=excluded.last_update
                    """,
                    (
                        order.order_id,
                        order.client_order_id,
                        order.cycle_id,
                        order.symbol,
                        order.side,
                        order.order_type,
                        str(order.price_try),
                        str(order.qty),
                        str(order.filled_qty),
                        (
                            str(order.avg_fill_price_try)
                            if order.avg_fill_price_try is not None
                            else None
                        ),
                        order.status.value,
                        order.intent_hash,
                        ensure_utc(order.last_update).isoformat(),
                    ),
                )

    def append_stage7_order_events(self, events: list[OrderEvent]) -> AppendResult:
        if not events:
            return AppendResult(attempted=0, inserted=0, ignored=0)
        inserted = 0
        with self.transaction() as conn:
            for event in events:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO stage7_order_events(
                        event_id, ts, cycle_id, order_id, client_order_id, event_type, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        ensure_utc(event.ts).isoformat(),
                        event.cycle_id,
                        event.order_id,
                        event.client_order_id,
                        event.event_type,
                        event.payload_json(),
                    ),
                )
                inserted += int(cur.rowcount > 0)
        attempted = len(events)
        return AppendResult(
            attempted=attempted, inserted=inserted, ignored=attempted - inserted
        )

    def append_stage7_order_event(self, event: OrderEvent) -> bool:
        """Append a single Stage7 order event with duplicate-event protection."""
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO stage7_order_events(
                    event_id, ts, cycle_id, order_id, client_order_id, event_type, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    ensure_utc(event.ts).isoformat(),
                    event.cycle_id,
                    event.order_id,
                    event.client_order_id,
                    event.event_type,
                    event.payload_json(),
                ),
            )
        return bool(cur.rowcount)

    def try_register_idempotency_key(self, key: str, payload_hash: str) -> bool:
        """Register an idempotency key atomically; return False for same-payload duplicates."""
        now_iso = ensure_utc(datetime.now(UTC)).isoformat()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT payload_hash FROM stage7_idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                existing_payload_hash = str(row["payload_hash"])
                if existing_payload_hash != payload_hash:
                    raise IdempotencyConflictError(
                        f"idempotency key conflict: {key}: "
                        f"{existing_payload_hash} != {payload_hash}"
                    )
                return False
            conn.execute(
                """
                INSERT INTO stage7_idempotency_keys(key, ts, payload_hash)
                VALUES (?, ?, ?)
                """,
                (key, now_iso, payload_hash),
            )
            return True

    def load_non_terminal_orders(self) -> list[Stage7Order]:
        """Load Stage7 orders whose status is not terminal."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM stage7_orders
                WHERE status NOT IN ('FILLED', 'CANCELED', 'REJECTED')
                ORDER BY last_update, client_order_id
                """
            ).fetchall()
        return [self._row_to_stage7_order(row) for row in rows]

    def load_order_events(self, client_order_id: str) -> list[OrderEvent]:
        """Load Stage7 order events for one client order id in storage order."""
        return self.get_stage7_order_events_by_client_id(client_order_id)

    def get_stage7_order_by_client_id(self, client_order_id: str) -> Stage7Order | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stage7_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_stage7_order(row)

    def get_stage7_order_events_by_client_id(
        self, client_order_id: str
    ) -> list[OrderEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM stage7_order_events
                WHERE client_order_id = ?
                ORDER BY ts, event_id
                """,
                (client_order_id,),
            ).fetchall()
        return [
            OrderEvent(
                event_id=str(row["event_id"]),
                ts=datetime.fromisoformat(str(row["ts"])),
                client_order_id=str(row["client_order_id"]),
                order_id=str(row["order_id"]),
                event_type=str(row["event_type"]),
                payload=json.loads(str(row["payload_json"])),
                cycle_id=str(row["cycle_id"]),
            )
            for row in rows
        ]

    def _row_to_stage7_order(self, row: sqlite3.Row) -> Stage7Order:
        avg_fill = row["avg_fill_price_try"]
        return Stage7Order(
            order_id=str(row["order_id"]),
            client_order_id=str(row["client_order_id"]),
            cycle_id=str(row["cycle_id"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            order_type=str(row["order_type"]),
            price_try=Decimal(str(row["price_try"])),
            qty=Decimal(str(row["qty"])),
            filled_qty=Decimal(str(row["filled_qty"])),
            avg_fill_price_try=Decimal(str(avg_fill)) if avg_fill is not None else None,
            status=Stage7OrderStatus(str(row["status"])),
            last_update=datetime.fromisoformat(str(row["last_update"])),
            intent_hash=str(row["intent_hash"]),
        )

    def _ensure_ledger_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger_events (
                event_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                type TEXT NOT NULL,
                side TEXT,
                qty TEXT NOT NULL,
                price TEXT,
                fee TEXT,
                fee_currency TEXT,
                exchange_trade_id TEXT,
                exchange_order_id TEXT,
                client_order_id TEXT,
                meta_json TEXT NOT NULL
            )
            """
        )
        # Dedupe scheme for exchange_trade_id:
        # - FILL events use raw exchange trade IDs (e.g., "t-123").
        # - FEE events use namespaced IDs (e.g., "fee:t-123") to avoid collisions.
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_events_exchange_trade_id_unique
            ON ledger_events(exchange_trade_id)
            WHERE exchange_trade_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_events_fallback_fill_unique
            ON ledger_events(client_order_id, symbol, side, price, qty, ts)
            WHERE type = 'FILL'
              AND exchange_trade_id IS NULL
              AND client_order_id IS NOT NULL
              AND side IS NOT NULL
              AND price IS NOT NULL
            """
        )

    def _ensure_stage4_schema(self, conn: sqlite3.Connection) -> None:
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
            "CREATE INDEX IF NOT EXISTS idx_stage4_orders_status ON stage4_orders(status)"
        )
        order_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(stage4_orders)")
        }
        if "mode" not in order_columns:
            conn.execute(
                "ALTER TABLE stage4_orders ADD COLUMN mode TEXT NOT NULL DEFAULT 'dry_run'"
            )
        if "last_error" not in order_columns:
            conn.execute("ALTER TABLE stage4_orders ADD COLUMN last_error TEXT")
        if "exchange_order_id" not in order_columns:
            conn.execute("ALTER TABLE stage4_orders ADD COLUMN exchange_order_id TEXT")
        if "exchange_client_id" not in order_columns:
            conn.execute("ALTER TABLE stage4_orders ADD COLUMN exchange_client_id TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stage4_orders_exchange_client_id_unique
            ON stage4_orders(exchange_client_id)
            WHERE exchange_client_id IS NOT NULL
            """
        )
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
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(pnl_snapshots)")
        }
        if "realized_total_try" not in columns:
            conn.execute(
                "ALTER TABLE pnl_snapshots ADD COLUMN realized_total_try TEXT NOT NULL DEFAULT '0'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pnl_snapshots_ts ON pnl_snapshots(ts)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cursors (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            CREATE TABLE IF NOT EXISTS allocation_plans (
                cycle_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                cash_try TEXT NOT NULL,
                try_cash_target TEXT NOT NULL,
                investable_total_try TEXT NOT NULL,
                investable_this_cycle_try TEXT NOT NULL,
                deploy_budget_try TEXT NOT NULL,
                planned_total_try TEXT NOT NULL,
                unused_budget_try TEXT NOT NULL,
                usage_reason TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                deferred_json TEXT NOT NULL DEFAULT '[]',
                decisions_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_allocation_plans_ts ON allocation_plans(ts)"
        )
        cycle_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(cycle_audit)")
        }
        if "envelope_json" not in cycle_columns:
            conn.execute("ALTER TABLE cycle_audit ADD COLUMN envelope_json TEXT")
        allocation_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(allocation_plans)")
        }
        if (
            "try_cash_target" not in allocation_columns
            and "cash_target_try" in allocation_columns
        ):
            conn.execute("ALTER TABLE allocation_plans ADD COLUMN try_cash_target TEXT")
            conn.execute(
                "UPDATE allocation_plans SET try_cash_target = cash_target_try"
            )
        if (
            "investable_total_try" not in allocation_columns
            and "investable_try" in allocation_columns
        ):
            conn.execute(
                "ALTER TABLE allocation_plans ADD COLUMN investable_total_try TEXT"
            )
            conn.execute(
                "UPDATE allocation_plans SET investable_total_try = investable_try"
            )
        if "investable_this_cycle_try" not in allocation_columns:
            conn.execute(
                "ALTER TABLE allocation_plans ADD COLUMN investable_this_cycle_try TEXT"
            )
            conn.execute(
                "UPDATE allocation_plans SET investable_this_cycle_try = "
                "COALESCE(investable_total_try, investable_try, '0')"
            )
        if "deploy_budget_try" not in allocation_columns:
            conn.execute(
                "ALTER TABLE allocation_plans ADD COLUMN deploy_budget_try TEXT"
            )
            conn.execute(
                "UPDATE allocation_plans SET deploy_budget_try = COALESCE(planned_total_try, '0')"
            )
        if (
            "unused_budget_try" not in allocation_columns
            and "unused_investable_try" in allocation_columns
        ):
            conn.execute(
                "ALTER TABLE allocation_plans ADD COLUMN unused_budget_try TEXT"
            )
            conn.execute(
                "UPDATE allocation_plans SET unused_budget_try = unused_investable_try"
            )
        if "deferred_json" not in allocation_columns:
            conn.execute("ALTER TABLE allocation_plans ADD COLUMN deferred_json TEXT")
            conn.execute(
                "UPDATE allocation_plans SET deferred_json = '[]' WHERE deferred_json IS NULL"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_snapshots (
                cycle_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                exchange TEXT NOT NULL,
                cash_try TEXT NOT NULL,
                total_equity_try TEXT NOT NULL,
                holdings_json TEXT NOT NULL,
                source_endpoints_json TEXT NOT NULL,
                flags_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_fills (
                fill_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capital_policy_state (
                state_key TEXT PRIMARY KEY,
                trading_capital_try TEXT NOT NULL,
                treasury_try TEXT NOT NULL,
                last_realized_pnl_total_try TEXT NOT NULL,
                last_checkpoint_id TEXT,
                last_cycle_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS universe_price_cache (
                pair_symbol TEXT NOT NULL,
                ts_bucket TEXT NOT NULL,
                mid_price TEXT NOT NULL,
                PRIMARY KEY(pair_symbol, ts_bucket)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dynamic_universe_cycles (
                cycle_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                selected_symbols_json TEXT NOT NULL,
                scores_json TEXT NOT NULL,
                filters_json TEXT NOT NULL,
                ineligible_counts_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dynamic_universe_cycles_ts "
            "ON dynamic_universe_cycles(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_universe_price_cache_pair_ts "
            "ON universe_price_cache(pair_symbol, ts_bucket)"
        )

    def _ensure_cycle_metrics_schema(self, conn: sqlite3.Connection) -> None:
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
            "CREATE INDEX IF NOT EXISTS idx_cycle_metrics_ts_start ON cycle_metrics(ts_start)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cycle_metrics_mode ON cycle_metrics(mode)"
        )

    def _ensure_actions_metadata_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(actions)")
        }
        if "client_order_id" not in columns:
            conn.execute("ALTER TABLE actions ADD COLUMN client_order_id TEXT")
        if "order_id" not in columns:
            conn.execute("ALTER TABLE actions ADD COLUMN order_id TEXT")
        if "metadata_json" not in columns:
            conn.execute("ALTER TABLE actions ADD COLUMN metadata_json TEXT")

    def _ensure_orders_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(orders)")
        }
        if "client_order_id" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN client_order_id TEXT")
        if "last_seen_at" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN last_seen_at INTEGER")
        if "reconciled" not in columns:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN reconciled INTEGER NOT NULL DEFAULT 0"
            )
        if "exchange_status_raw" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN exchange_status_raw TEXT")
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
        if "intent_id" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN intent_id TEXT")
        if "unknown_first_seen_at" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN unknown_first_seen_at INTEGER")
        if "unknown_last_probe_at" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN unknown_last_probe_at INTEGER")
        if "unknown_next_probe_at" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN unknown_next_probe_at INTEGER")
        if "unknown_probe_attempts" not in columns:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN unknown_probe_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "unknown_escalated_at" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN unknown_escalated_at INTEGER")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_order_id_unique
            ON orders(client_order_id)
            WHERE client_order_id IS NOT NULL
            """
        )

    def _ensure_idempotency_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                action_type TEXT NOT NULL,
                key TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at_epoch INTEGER NOT NULL,
                expires_at_epoch INTEGER NOT NULL,
                action_id INTEGER,
                client_order_id TEXT,
                order_id TEXT,
                status TEXT NOT NULL,
                recovery_attempts INTEGER NOT NULL DEFAULT 0,
                next_recovery_at_epoch INTEGER,
                PRIMARY KEY (action_type, key)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_idempotency_keys_expires_at
            ON idempotency_keys(expires_at_epoch)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_idempotency_keys_status_expires
            ON idempotency_keys(status, expires_at_epoch)
            """
        )
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(idempotency_keys)")}
        if "recovery_attempts" not in columns:
            conn.execute(
                "ALTER TABLE idempotency_keys ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "next_recovery_at_epoch" not in columns:
            conn.execute(
                "ALTER TABLE idempotency_keys ADD COLUMN next_recovery_at_epoch INTEGER"
            )

    def record_action(
        self,
        cycle_id: str,
        action_type: str,
        payload_hash: str,
        dedupe_window_seconds: int = 300,
        dedupe_key: str | None = None,
    ) -> int | None:
        now_epoch = int(datetime.now(UTC).timestamp())
        resolved_dedupe_key = dedupe_key
        if resolved_dedupe_key is None:
            dedupe_window = max(1, dedupe_window_seconds)
            dedupe_bucket = now_epoch // dedupe_window
            resolved_dedupe_key = f"{action_type}:{payload_hash}:{dedupe_bucket}"

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO actions (
                    cycle_id, action_type, payload_hash, dedupe_key, created_at_epoch
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (cycle_id, action_type, payload_hash, resolved_dedupe_key, now_epoch),
            )
            if cursor.rowcount == 0:
                return None
            return int(cursor.lastrowid)

    def attach_action_metadata(
        self,
        *,
        action_id: int,
        client_order_id: str | None,
        order_id: str | None,
        reconciled: bool,
        reconcile_status: str | None,
        reconcile_reason: str | None,
        idempotency_key: str | None = None,
        intent_id: str | None = None,
    ) -> None:
        metadata_payload = {
            "reconciled": reconciled,
            "reconcile_status": reconcile_status,
            "reconcile_reason": reconcile_reason,
            "idempotency_key": idempotency_key,
            "intent_id": intent_id,
        }
        metadata_json = json.dumps(metadata_payload, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE actions
                SET client_order_id = ?, order_id = ?, metadata_json = ?
                WHERE id = ?
                """,
                (client_order_id, order_id, metadata_json, action_id),
            )

    def action_count(self, action_type: str, payload_hash: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM actions
                WHERE action_type = ? AND payload_hash = ?
                """,
                (action_type, payload_hash),
            ).fetchone()
        return int(row["count"] if row else 0)

    def get_action_by_id(self, action_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()

    def get_action_by_dedupe_key(self, dedupe_key: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM actions WHERE dedupe_key = ? ORDER BY id DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()

    def clear_action_dedupe_key(self, action_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE actions SET dedupe_key = NULL WHERE id = ?",
                (action_id,),
            )

    def get_latest_action(
        self, action_type: str, payload_hash: str
    ) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM actions
                WHERE action_type = ? AND payload_hash = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (action_type, payload_hash),
            ).fetchone()

    def save_order(
        self,
        order: Order,
        *,
        reconciled: bool = False,
        exchange_status_raw: str | None = None,
        idempotency_key: str | None = None,
        intent_id: str | None = None,
    ) -> None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        with self._connect() as conn:
            payload = (
                order.order_id,
                normalize_symbol(order.symbol),
                order.client_order_id,
                order.side.value,
                str(Decimal(str(order.price))),
                str(Decimal(str(order.quantity))),
                order.status.value,
                order.created_at.isoformat(),
                order.updated_at.isoformat(),
                now_ms,
                1 if reconciled else 0,
                exchange_status_raw,
                idempotency_key,
                intent_id,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO orders (
                        order_id, symbol, client_order_id, side, price, qty, status,
                        created_at, updated_at, last_seen_at, reconciled, exchange_status_raw,
                        idempotency_key, intent_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        client_order_id=excluded.client_order_id,
                        status=excluded.status,
                        updated_at=excluded.updated_at,
                        last_seen_at=excluded.last_seen_at,
                        reconciled=excluded.reconciled,
                        exchange_status_raw=excluded.exchange_status_raw,
                        idempotency_key=COALESCE(excluded.idempotency_key, orders.idempotency_key),
                        intent_id=COALESCE(excluded.intent_id, orders.intent_id)
                    """,
                    payload,
                )
            except sqlite3.IntegrityError:
                if not order.client_order_id:
                    raise
                existing = conn.execute(
                    "SELECT order_id FROM orders WHERE client_order_id = ?",
                    (order.client_order_id,),
                ).fetchone()
                if existing is None:
                    raise
                conn.execute(
                    """
                    UPDATE orders
                    SET order_id = ?,
                        symbol = ?,
                        side = ?,
                        price = ?,
                        qty = ?,
                        status = ?,
                        updated_at = ?,
                        last_seen_at = ?,
                        reconciled = ?,
                        exchange_status_raw = ?,
                        idempotency_key = COALESCE(?, idempotency_key),
                        intent_id = COALESCE(?, intent_id)
                    WHERE order_id = ?
                    """,
                    (
                        order.order_id,
                        normalize_symbol(order.symbol),
                        order.side.value,
                        str(Decimal(str(order.price))),
                        str(Decimal(str(order.quantity))),
                        order.status.value,
                        order.updated_at.isoformat(),
                        now_ms,
                        1 if reconciled else 0,
                        exchange_status_raw,
                        idempotency_key,
                        intent_id,
                        str(existing["order_id"]),
                    ),
                )

    def reserve_idempotency_key(
        self,
        action_type: str,
        key: str,
        payload_hash: str,
        ttl_seconds: int,
        allow_promote_simulated: bool = False,
    ) -> ReservationResult:
        now_epoch = int(datetime.now(UTC).timestamp())
        expires_at = now_epoch + max(1, ttl_seconds)
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency_keys(
                        action_type, key, payload_hash, created_at_epoch,
                        expires_at_epoch, status
                    ) VALUES (?, ?, ?, ?, ?, 'PENDING')
                    """,
                    (action_type, key, payload_hash, now_epoch, expires_at),
                )
                row = conn.execute(
                    """
                    SELECT * FROM idempotency_keys
                    WHERE action_type = ? AND key = ?
                    """,
                    (action_type, key),
                ).fetchone()
                assert row is not None
                return self._row_to_reservation_result(row, reserved=True)
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT * FROM idempotency_keys
                    WHERE action_type = ? AND key = ?
                    """,
                    (action_type, key),
                ).fetchone()
                if row is None:
                    raise
                if str(row["payload_hash"]) != payload_hash:
                    raise IdempotencyConflictError(
                        f"idempotency key conflict for {action_type}:{key}: "
                        f"existing={row['payload_hash']} incoming={payload_hash}"
                    )
                status = str(row["status"]).upper()
                age_seconds = max(0, now_epoch - int(row["created_at_epoch"]))
                if status == "PENDING" and age_seconds > PENDING_GRACE_SECONDS:
                    if row["action_id"] is None and row["client_order_id"] is None:
                        conn.execute(
                            """
                            UPDATE idempotency_keys
                            SET status = 'FAILED'
                            WHERE action_type = ? AND key = ?
                            """,
                            (action_type, key),
                        )
                        row = conn.execute(
                            """
                            SELECT * FROM idempotency_keys
                            WHERE action_type = ? AND key = ?
                            """,
                            (action_type, key),
                        ).fetchone()
                        if row is None:
                            raise RuntimeError(
                                f"failed to mark stale idempotency key failed {action_type}:{key}"
                            )
                        status = str(row["status"]).upper()
                if (
                    allow_promote_simulated
                    and status == "SIMULATED"
                ):
                    conn.execute(
                        """
                        UPDATE idempotency_keys
                        SET status = 'PENDING',
                            created_at_epoch = ?,
                            expires_at_epoch = ?,
                            action_id = NULL,
                            client_order_id = NULL,
                            order_id = NULL,
                            recovery_attempts = 0,
                            next_recovery_at_epoch = NULL
                        WHERE action_type = ? AND key = ?
                        """,
                        (now_epoch, expires_at, action_type, key),
                    )
                    promoted = conn.execute(
                        """
                        SELECT * FROM idempotency_keys
                        WHERE action_type = ? AND key = ?
                        """,
                        (action_type, key),
                    ).fetchone()
                    if promoted is None:
                        raise RuntimeError(
                            f"failed to promote simulated idempotency key {action_type}:{key}"
                        )
                    return self._row_to_reservation_result(promoted, reserved=True)
                if status == "FAILED":
                    conn.execute(
                        """
                        UPDATE idempotency_keys
                        SET status = 'PENDING',
                            created_at_epoch = ?,
                            expires_at_epoch = ?,
                            action_id = NULL,
                            client_order_id = NULL,
                            order_id = NULL,
                            recovery_attempts = 0,
                            next_recovery_at_epoch = NULL
                        WHERE action_type = ? AND key = ?
                        """,
                        (now_epoch, expires_at, action_type, key),
                    )
                    retry_row = conn.execute(
                        """
                        SELECT * FROM idempotency_keys
                        WHERE action_type = ? AND key = ?
                        """,
                        (action_type, key),
                    ).fetchone()
                    if retry_row is None:
                        raise RuntimeError(
                            f"failed to re-reserve failed idempotency key {action_type}:{key}"
                        )
                    return self._row_to_reservation_result(retry_row, reserved=True)
                return self._row_to_reservation_result(row, reserved=False)

    def finalize_idempotency_key(
        self,
        action_type: str,
        key: str,
        *,
        action_id: int | None,
        client_order_id: str | None,
        order_id: str | None,
        status: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE idempotency_keys
                SET action_id = COALESCE(?, action_id),
                    client_order_id = COALESCE(?, client_order_id),
                    order_id = COALESCE(?, order_id),
                    status = ?
                WHERE action_type = ? AND key = ?
                """,
                (action_id, client_order_id, order_id, status, action_type, key),
            )

    def update_idempotency_recovery(
        self,
        action_type: str,
        key: str,
        *,
        recovery_attempts: int,
        next_recovery_at_epoch: int | None,
        status: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE idempotency_keys
                SET recovery_attempts = ?,
                    next_recovery_at_epoch = ?,
                    status = COALESCE(?, status)
                WHERE action_type = ? AND key = ?
                """,
                (
                    max(0, recovery_attempts),
                    next_recovery_at_epoch,
                    status,
                    action_type,
                    key,
                ),
            )

    def prune_expired_idempotency_keys(self, now_epoch: int | None = None) -> int:
        resolved_now = now_epoch or int(datetime.now(UTC).timestamp())
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM idempotency_keys WHERE expires_at_epoch <= ?",
                (resolved_now,),
            )
            return int(cur.rowcount)

    def _row_to_reservation_result(
        self, row: sqlite3.Row, *, reserved: bool
    ) -> ReservationResult:
        return ReservationResult(
            reserved=reserved,
            action_type=str(row["action_type"]),
            key=str(row["key"]),
            payload_hash=str(row["payload_hash"]),
            created_at_epoch=int(row["created_at_epoch"]),
            expires_at_epoch=int(row["expires_at_epoch"]),
            action_id=int(row["action_id"]) if row["action_id"] is not None else None,
            client_order_id=(
                str(row["client_order_id"])
                if row["client_order_id"] is not None
                else None
            ),
            order_id=str(row["order_id"]) if row["order_id"] is not None else None,
            status=str(row["status"]),
            recovery_attempts=int(row["recovery_attempts"] or 0),
            next_recovery_at_epoch=(
                int(row["next_recovery_at_epoch"])
                if row["next_recovery_at_epoch"] is not None
                else None
            ),
        )

    def save_fill(self, fill: TradeFill) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO fills (
                    fill_id, order_id, symbol, side, price, qty, fee, fee_currency, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    normalize_symbol(fill.symbol),
                    fill.side.value,
                    str(fill.price),
                    str(fill.qty),
                    str(fill.fee),
                    fill.fee_currency,
                    fill.ts.isoformat(),
                ),
            )
        return bool(cur.rowcount)

    def save_position(self, position: Position) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO positions (
                    symbol, qty, avg_cost, realized_pnl, unrealized_pnl, fees_paid, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    qty=excluded.qty,
                    avg_cost=excluded.avg_cost,
                    realized_pnl=excluded.realized_pnl,
                    unrealized_pnl=excluded.unrealized_pnl,
                    fees_paid=excluded.fees_paid,
                    updated_at=excluded.updated_at
                """,
                (
                    normalize_symbol(position.symbol),
                    str(position.qty),
                    str(position.avg_cost),
                    str(position.realized_pnl),
                    str(position.unrealized_pnl),
                    str(position.fees_paid),
                    position.updated_at.isoformat(),
                ),
            )

    def get_position(self, symbol: str) -> Position | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE symbol = ?", (normalize_symbol(symbol),)
            ).fetchone()
        if row is None:
            return None
        return Position(
            symbol=str(row["symbol"]),
            qty=Decimal(str(row["qty"])),
            avg_cost=Decimal(str(row["avg_cost"])),
            realized_pnl=Decimal(str(row["realized_pnl"])),
            unrealized_pnl=Decimal(str(row["unrealized_pnl"])),
            fees_paid=Decimal(str(row["fees_paid"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def get_positions(self) -> list[Position]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [
            Position(
                symbol=str(row["symbol"]),
                qty=Decimal(str(row["qty"])),
                avg_cost=Decimal(str(row["avg_cost"])),
                realized_pnl=Decimal(str(row["realized_pnl"])),
                unrealized_pnl=Decimal(str(row["unrealized_pnl"])),
                fees_paid=Decimal(str(row["fees_paid"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        ]

    def record_intent(self, intent: Intent, ts: datetime | None = None) -> None:
        created_at = (ts or intent.created_at).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO intents (intent_id, symbol, side, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    normalize_symbol(intent.symbol),
                    intent.side.value,
                    intent.idempotency_key,
                    created_at,
                ),
            )

    def get_last_intent_ts_by_symbol_side(self) -> dict[tuple[str, str], datetime]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, side, MAX(created_at) as created_at
                FROM intents
                GROUP BY symbol, side
                """
            ).fetchall()
        return {
            (str(row["symbol"]), str(row["side"])): datetime.fromisoformat(
                str(row["created_at"])
            )
            for row in rows
        }

    def update_order_status(
        self,
        *,
        order_id: str,
        status: OrderStatus,
        exchange_status_raw: str | None = None,
        reconciled: bool | None = None,
        last_seen_at: int | None = None,
    ) -> None:
        now_iso = datetime.now(UTC).isoformat()
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        is_unknown = 1 if status == OrderStatus.UNKNOWN else 0
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status = ?,
                    updated_at = ?,
                    last_seen_at = COALESCE(?, last_seen_at),
                    exchange_status_raw = COALESCE(?, exchange_status_raw),
                    reconciled = COALESCE(?, reconciled),
                    unknown_first_seen_at = CASE
                        WHEN ? = 1 THEN COALESCE(unknown_first_seen_at, ?)
                        ELSE NULL
                    END,
                    unknown_last_probe_at = CASE
                        WHEN ? = 1 THEN unknown_last_probe_at
                        ELSE NULL
                    END,
                    unknown_next_probe_at = CASE
                        WHEN ? = 1 THEN COALESCE(unknown_next_probe_at, ?)
                        ELSE NULL
                    END,
                    unknown_probe_attempts = CASE
                        WHEN ? = 1 THEN COALESCE(unknown_probe_attempts, 0)
                        ELSE 0
                    END,
                    unknown_escalated_at = CASE
                        WHEN ? = 1 THEN unknown_escalated_at
                        ELSE NULL
                    END
                WHERE order_id = ?
                """,
                (
                    status.value,
                    now_iso,
                    last_seen_at,
                    exchange_status_raw,
                    (1 if reconciled else 0) if reconciled is not None else None,
                    is_unknown,
                    now_ms,
                    is_unknown,
                    is_unknown,
                    now_ms,
                    is_unknown,
                    is_unknown,
                    order_id,
                ),
            )

    def mark_unknown_probe_result(
        self,
        *,
        order_id: str,
        last_probe_at: int,
        next_probe_at: int,
        escalate: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE orders
                SET unknown_first_seen_at = COALESCE(unknown_first_seen_at, ?),
                    unknown_last_probe_at = ?,
                    unknown_next_probe_at = ?,
                    unknown_probe_attempts = COALESCE(unknown_probe_attempts, 0) + 1,
                    unknown_escalated_at = CASE
                        WHEN ? THEN COALESCE(unknown_escalated_at, ?)
                        ELSE unknown_escalated_at
                    END
                WHERE order_id = ?
                """,
                (
                    last_probe_at,
                    last_probe_at,
                    next_probe_at,
                    1 if escalate else 0,
                    last_probe_at,
                    order_id,
                ),
            )

    def find_open_or_unknown_orders(
        self, symbols: list[str] | None = None
    ) -> list[StoredOrder]:
        query = """
            SELECT order_id, symbol, client_order_id, side, price, qty, status,
                   last_seen_at, reconciled, exchange_status_raw,
                   unknown_first_seen_at, unknown_last_probe_at, unknown_next_probe_at,
                   unknown_probe_attempts, unknown_escalated_at
            FROM orders
            WHERE status IN ('new', 'open', 'partial', 'unknown')
        """
        params: list[str] = []
        if symbols:
            normalized = [normalize_symbol(value) for value in symbols]
            query += f" AND symbol IN ({','.join('?' for _ in normalized)})"
            params.extend(normalized)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            StoredOrder(
                order_id=str(row["order_id"]),
                symbol=str(row["symbol"]),
                client_order_id=row["client_order_id"],
                side=str(row["side"]),
                price=Decimal(str(row["price"])),
                quantity=Decimal(str(row["qty"])),
                status=OrderStatus(str(row["status"])),
                last_seen_at=(
                    int(row["last_seen_at"])
                    if row["last_seen_at"] is not None
                    else None
                ),
                reconciled=bool(row["reconciled"]),
                exchange_status_raw=row["exchange_status_raw"],
                unknown_first_seen_at=(
                    int(row["unknown_first_seen_at"])
                    if row["unknown_first_seen_at"] is not None
                    else None
                ),
                unknown_last_probe_at=(
                    int(row["unknown_last_probe_at"])
                    if row["unknown_last_probe_at"] is not None
                    else None
                ),
                unknown_next_probe_at=(
                    int(row["unknown_next_probe_at"])
                    if row["unknown_next_probe_at"] is not None
                    else None
                ),
                unknown_probe_attempts=int(row["unknown_probe_attempts"] or 0),
                unknown_escalated_at=(
                    int(row["unknown_escalated_at"])
                    if row["unknown_escalated_at"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def mark_order_canceled(self, order_id: str) -> None:
        self.update_order_status(order_id=order_id, status=OrderStatus.CANCELED)

    def set_last_cycle_id(self, cycle_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES ('last_cycle_id', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (cycle_id,),
            )

    def get_last_cycle_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='last_cycle_id'"
            ).fetchone()
        return row["value"] if row else None

    def set_last_stage7_cycle_id(self, cycle_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES ('last_stage7_cycle_id', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (cycle_id,),
            )

    def get_latest_risk_mode(self) -> Mode:
        from btcbot.domain.risk_budget import Mode

        with self._connect() as conn:
            row = conn.execute(
                "SELECT mode FROM risk_decisions ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return Mode.NORMAL
        try:
            return Mode(str(row["mode"]))
        except ValueError:
            return Mode.NORMAL

    # Stage 4 helpers
    def client_order_id_exists(self, client_order_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM stage4_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return row is not None

    def get_stage4_order_by_client_id(self, client_order_id: str):
        """Load a Stage4 order by client_order_id."""
        from btcbot.domain.stage4 import Order as Stage4Order

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stage4_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        if row is None:
            return None
        return Stage4Order(
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            type="limit",
            price=Decimal(str(row["price"])),
            qty=Decimal(str(row["qty"])),
            status=str(row["status"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            exchange_order_id=(
                str(row["exchange_order_id"]) if row["exchange_order_id"] else None
            ),
            client_order_id=(
                str(row["client_order_id"]) if row["client_order_id"] else None
            ),
            exchange_client_id=(
                str(row["exchange_client_id"]) if row["exchange_client_id"] else None
            ),
            mode=str(row["mode"]),
        )

    def list_stage4_open_orders(
        self,
        symbol: str | None = None,
        *,
        include_external: bool = False,
    ):
        from btcbot.domain.stage4 import Order as Stage4Order

        query = "SELECT * FROM stage4_orders WHERE status IN ('open','submitted','cancel_requested')"
        if not include_external:
            query += " AND mode != 'external'"
        params: list[str] = []
        if symbol is not None:
            query += " AND symbol = ?"
            params.append(normalize_symbol(symbol))
        query += " ORDER BY symbol, side, created_at"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            Stage4Order(
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                type="limit",
                price=Decimal(str(row["price"])),
                qty=Decimal(str(row["qty"])),
                status=str(row["status"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
                exchange_order_id=(
                    str(row["exchange_order_id"]) if row["exchange_order_id"] else None
                ),
                client_order_id=(
                    str(row["client_order_id"]) if row["client_order_id"] else None
                ),
                exchange_client_id=(
                    str(row["exchange_client_id"])
                    if row["exchange_client_id"]
                    else None
                ),
                mode=str(row["mode"]),
            )
            for row in rows
        ]

    def is_order_terminal(self, client_order_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM stage4_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        if row is None:
            return False
        return str(row["status"]).lower() in {
            "filled",
            "canceled",
            "rejected",
            "unknown_closed",
        }

    def stage4_submit_dedupe_status(
        self,
        *,
        internal_client_order_id: str,
        exchange_client_order_id: str,
    ) -> SubmitDedupeDecision:
        with self._connect() as conn:
            row = conn.execute(
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
            return SubmitDedupeDecision(
                should_dedupe=False, dedupe_key=exchange_client_order_id
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

        return SubmitDedupeDecision(
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
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM stage4_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
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
                conn.execute(
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
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE stage4_orders
                SET status='cancel_requested', updated_at=?
                WHERE client_order_id=? AND status IN ('open','submitted')
                """,
                (datetime.now(UTC).isoformat(), client_order_id),
            )

    def record_stage4_order_canceled(self, client_order_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
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
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM stage4_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
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
                conn.execute(
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

    def update_stage4_order_exchange_id(
        self, client_order_id: str, exchange_order_id: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE stage4_orders
                SET exchange_order_id=?, updated_at=?
                WHERE client_order_id=?
                """,
                (exchange_order_id, datetime.now(UTC).isoformat(), client_order_id),
            )

    def mark_stage4_unknown_closed(self, client_order_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE stage4_orders
                SET status='unknown_closed', updated_at=?
                WHERE client_order_id=?
                """,
                (datetime.now(UTC).isoformat(), client_order_id),
            )

    def import_stage4_external_order(self, order) -> None:
        client_order_id = getattr(order, "client_order_id", None)
        exchange_order_id = getattr(order, "exchange_order_id", None)
        if exchange_order_id is None:
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = None
            if client_order_id is not None:
                existing = conn.execute(
                    "SELECT id, exchange_order_id FROM stage4_orders WHERE client_order_id = ?",
                    (client_order_id,),
                ).fetchone()
            if existing is None:
                conn.execute(
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
                conn.execute(
                    """
                    UPDATE stage4_orders
                    SET exchange_order_id=COALESCE(exchange_order_id, ?), updated_at=?
                    WHERE client_order_id=?
                    """,
                    (exchange_order_id, now, client_order_id),
                )

    def get_stage4_order_by_exchange_id(self, exchange_order_id: str):
        from btcbot.domain.stage4 import Order as Stage4Order

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stage4_orders WHERE exchange_order_id = ?",
                (exchange_order_id,),
            ).fetchone()
        if row is None:
            return None
        return Stage4Order(
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            type="limit",
            price=Decimal(str(row["price"])),
            qty=Decimal(str(row["qty"])),
            status=str(row["status"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            exchange_order_id=(
                str(row["exchange_order_id"]) if row["exchange_order_id"] else None
            ),
            client_order_id=(
                str(row["client_order_id"]) if row["client_order_id"] else None
            ),
            exchange_client_id=(
                str(row["exchange_client_id"]) if row["exchange_client_id"] else None
            ),
            mode=str(row["mode"]),
        )

    def save_stage4_fill(self, fill: Stage4Fill) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO stage4_fills(
                    fill_id, order_id, symbol, side, price, qty, fee, fee_asset, ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    normalize_symbol(fill.symbol),
                    fill.side,
                    str(fill.price),
                    str(fill.qty),
                    str(fill.fee),
                    fill.fee_asset,
                    fill.ts.isoformat(),
                ),
            )
        return bool(cur.rowcount)

    def mark_fill_applied(self, fill_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO applied_fills(fill_id) VALUES (?)",
                (fill_id,),
            )
        return bool(cur.rowcount)

    def get_capital_policy_state(self) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM capital_policy_state WHERE state_key = 'primary'"
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_capital_policy_state(
        self,
        *,
        trading_capital_try: Decimal,
        treasury_try: Decimal,
        last_realized_pnl_total_try: Decimal,
        last_checkpoint_id: str | None,
        last_cycle_id: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO capital_policy_state(
                    state_key,
                    trading_capital_try,
                    treasury_try,
                    last_realized_pnl_total_try,
                    last_checkpoint_id,
                    last_cycle_id,
                    updated_at
                )
                VALUES ('primary', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    trading_capital_try=excluded.trading_capital_try,
                    treasury_try=excluded.treasury_try,
                    last_realized_pnl_total_try=excluded.last_realized_pnl_total_try,
                    last_checkpoint_id=excluded.last_checkpoint_id,
                    last_cycle_id=excluded.last_cycle_id,
                    updated_at=excluded.updated_at
                """,
                (
                    str(trading_capital_try),
                    str(treasury_try),
                    str(last_realized_pnl_total_try),
                    last_checkpoint_id,
                    last_cycle_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def save_stage4_position(self, position: Stage4Position) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO stage4_positions(
                    symbol, qty, avg_cost_try, realized_pnl_try, last_update_ts
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    qty=excluded.qty,
                    avg_cost_try=excluded.avg_cost_try,
                    realized_pnl_try=excluded.realized_pnl_try,
                    last_update_ts=excluded.last_update_ts
                """,
                (
                    normalize_symbol(position.symbol),
                    str(position.qty),
                    str(position.avg_cost_try),
                    str(position.realized_pnl_try),
                    position.last_update_ts.isoformat(),
                ),
            )

    def get_stage4_position(self, symbol: str) -> Stage4Position | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stage4_positions WHERE symbol=?",
                (normalize_symbol(symbol),),
            ).fetchone()
        if row is None:
            return None
        return Stage4Position(
            symbol=str(row["symbol"]),
            qty=Decimal(str(row["qty"])),
            avg_cost_try=Decimal(str(row["avg_cost_try"])),
            realized_pnl_try=Decimal(str(row["realized_pnl_try"])),
            last_update_ts=datetime.fromisoformat(str(row["last_update_ts"])),
        )

    def list_stage4_positions(self) -> list[Stage4Position]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stage4_positions ORDER BY symbol"
            ).fetchall()
        return [
            Stage4Position(
                symbol=str(row["symbol"]),
                qty=Decimal(str(row["qty"])),
                avg_cost_try=Decimal(str(row["avg_cost_try"])),
                realized_pnl_try=Decimal(str(row["realized_pnl_try"])),
                last_update_ts=datetime.fromisoformat(str(row["last_update_ts"])),
            )
            for row in rows
        ]

    def save_stage4_pnl_snapshot(self, snapshot: PnLSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pnl_snapshots(
                    total_equity_try, realized_today_try, realized_total_try, drawdown_pct, ts
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(snapshot.total_equity_try),
                    str(snapshot.realized_today_try),
                    str(snapshot.realized_total_try),
                    str(snapshot.drawdown_pct),
                    snapshot.ts.isoformat(),
                ),
            )

    def realized_total_at_day_start(self, day_start: datetime) -> Decimal:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT realized_total_try
                FROM pnl_snapshots
                WHERE ts >= ?
                ORDER BY ts ASC
                LIMIT 1
                """,
                (day_start.isoformat(),),
            ).fetchone()
        return Decimal(str(row["realized_total_try"])) if row else Decimal("0")

    def compute_drawdown_pct(self, equity_now: Decimal) -> Decimal:
        with self._connect() as conn:
            rows = conn.execute("SELECT total_equity_try FROM pnl_snapshots").fetchall()
        values = [Decimal(str(row["total_equity_try"])) for row in rows]
        peak = max(values + [equity_now]) if values else equity_now
        if peak <= 0:
            return Decimal("0")
        return max(Decimal("0"), ((peak - equity_now) / peak) * Decimal("100"))

    def save_allocation_plan(
        self,
        *,
        cycle_id: str,
        ts: datetime,
        cash_try: Decimal,
        try_cash_target: Decimal,
        investable_total_try: Decimal,
        investable_this_cycle_try: Decimal,
        deploy_budget_try: Decimal,
        planned_total_try: Decimal,
        unused_budget_try: Decimal,
        usage_reason: str,
        plan: list[dict[str, object]],
        deferred: list[dict[str, object]],
        decisions: list[dict[str, object]],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO allocation_plans(
                    cycle_id, ts, cash_try, try_cash_target, investable_total_try,
                    investable_this_cycle_try, deploy_budget_try, planned_total_try,
                    unused_budget_try, usage_reason, plan_json, deferred_json, decisions_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    ts=excluded.ts,
                    cash_try=excluded.cash_try,
                    try_cash_target=excluded.try_cash_target,
                    investable_total_try=excluded.investable_total_try,
                    investable_this_cycle_try=excluded.investable_this_cycle_try,
                    deploy_budget_try=excluded.deploy_budget_try,
                    planned_total_try=excluded.planned_total_try,
                    unused_budget_try=excluded.unused_budget_try,
                    usage_reason=excluded.usage_reason,
                    plan_json=excluded.plan_json,
                    deferred_json=excluded.deferred_json,
                    decisions_json=excluded.decisions_json
                """,
                (
                    cycle_id,
                    ensure_utc(ts).isoformat(),
                    str(cash_try),
                    str(try_cash_target),
                    str(investable_total_try),
                    str(investable_this_cycle_try),
                    str(deploy_budget_try),
                    str(planned_total_try),
                    str(unused_budget_try),
                    usage_reason,
                    json.dumps(plan, sort_keys=True),
                    json.dumps(deferred, sort_keys=True),
                    json.dumps(decisions, sort_keys=True),
                ),
            )

    def get_allocation_plan(self, cycle_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM allocation_plans WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
        if row is None:
            return None
        payload = {key: row[key] for key in row.keys()}
        payload["plan"] = json.loads(str(payload.pop("plan_json")))
        payload["deferred"] = json.loads(str(payload.pop("deferred_json") or "[]"))
        payload["decisions"] = json.loads(str(payload.pop("decisions_json")))
        return payload

    def get_latest_allocation_plan(self) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM allocation_plans ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        payload = {key: row[key] for key in row.keys()}
        payload["plan"] = json.loads(str(payload.pop("plan_json")))
        payload["deferred"] = json.loads(str(payload.pop("deferred_json") or "[]"))
        payload["decisions"] = json.loads(str(payload.pop("decisions_json")))
        return payload

    def upsert_universe_price_snapshot(
        self,
        *,
        pair_symbol: str,
        ts_bucket: datetime,
        mid_price: Decimal,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO universe_price_cache(pair_symbol, ts_bucket, mid_price)
                VALUES (?, ?, ?)
                ON CONFLICT(pair_symbol, ts_bucket) DO UPDATE SET
                    mid_price=excluded.mid_price
                """,
                (pair_symbol, ensure_utc(ts_bucket).isoformat(), str(mid_price)),
            )

    def get_universe_price_lookback(
        self,
        *,
        pair_symbol: str,
        target_ts: datetime,
        tolerance: timedelta,
    ) -> Decimal | None:
        target = ensure_utc(target_ts)
        start = (target - tolerance).isoformat()
        end = (target + tolerance).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT mid_price
                FROM universe_price_cache
                WHERE pair_symbol = ?
                  AND ts_bucket BETWEEN ? AND ?
                ORDER BY ABS(strftime('%s', ts_bucket) - strftime('%s', ?)) ASC, ts_bucket ASC
                LIMIT 1
                """,
                (pair_symbol, start, end, target.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return Decimal(str(row["mid_price"]))

    def save_dynamic_universe_selection(
        self,
        *,
        cycle_id: str,
        ts: datetime,
        selected_symbols: list[str],
        scores: dict[str, str],
        filters: dict[str, object],
        ineligible_counts: dict[str, int],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dynamic_universe_cycles(
                    cycle_id, ts, selected_symbols_json, scores_json, filters_json,
                    ineligible_counts_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    ts=excluded.ts,
                    selected_symbols_json=excluded.selected_symbols_json,
                    scores_json=excluded.scores_json,
                    filters_json=excluded.filters_json,
                    ineligible_counts_json=excluded.ineligible_counts_json
                """,
                (
                    cycle_id,
                    ensure_utc(ts).isoformat(),
                    json.dumps(selected_symbols, sort_keys=True),
                    json.dumps(scores, sort_keys=True),
                    json.dumps(filters, sort_keys=True),
                    json.dumps(ineligible_counts, sort_keys=True),
                ),
            )

    def get_latest_dynamic_universe_selection(self) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM dynamic_universe_cycles ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        payload = {key: row[key] for key in row.keys()}
        payload["selected_symbols"] = json.loads(
            str(payload.pop("selected_symbols_json") or "[]")
        )
        payload["scores"] = json.loads(str(payload.pop("scores_json") or "{}"))
        payload["filters"] = json.loads(str(payload.pop("filters_json") or "{}"))
        payload["ineligible_counts"] = json.loads(
            str(payload.pop("ineligible_counts_json") or "{}")
        )
        return payload

    def record_cycle_audit(
        self,
        cycle_id: str,
        counts: dict[str, int],
        decisions: list[str],
        envelope: dict[str, object] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
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
                    (
                        json.dumps(envelope, sort_keys=True)
                        if envelope is not None
                        else None
                    ),
                ),
            )

    def save_account_snapshot(
        self, *, cycle_id: str, snapshot: AccountSnapshot
    ) -> None:
        holdings_payload = {
            asset: {"free": str(item.free), "locked": str(item.locked)}
            for asset, item in snapshot.holdings.items()
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO account_snapshots(
                    cycle_id, ts, exchange, cash_try, total_equity_try,
                    holdings_json, source_endpoints_json, flags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    snapshot.timestamp.isoformat(),
                    snapshot.exchange,
                    str(snapshot.cash_try),
                    str(snapshot.total_equity_try),
                    json.dumps(holdings_payload, sort_keys=True),
                    json.dumps(list(snapshot.source_endpoints), sort_keys=True),
                    json.dumps(list(snapshot.flags), sort_keys=True),
                ),
            )

    def get_account_snapshot(self, cycle_id: str) -> AccountSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_snapshots WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
        if row is None:
            return None
        holdings_data = json.loads(str(row["holdings_json"]))
        holdings: dict[str, Holding] = {}
        for asset, payload in holdings_data.items():
            holdings[str(asset)] = Holding(
                asset=str(asset),
                free=Decimal(str(payload.get("free", "0"))),
                locked=Decimal(str(payload.get("locked", "0"))),
            )
        return AccountSnapshot(
            timestamp=datetime.fromisoformat(str(row["ts"])),
            exchange=str(row["exchange"]),
            cash_try=Decimal(str(row["cash_try"])),
            holdings=holdings,
            total_equity_try=Decimal(str(row["total_equity_try"])),
            source_endpoints=tuple(json.loads(str(row["source_endpoints_json"]))),
            flags=tuple(json.loads(str(row["flags_json"]))),
        )

    def append_ledger_events(self, events: list[LedgerEvent]) -> AppendResult:
        inserted = 0
        with self._connect() as conn:
            for event in events:
                event_ts = ensure_utc(event.ts).isoformat()
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO ledger_events(
                        event_id, ts, symbol, type, side, qty, price, fee, fee_currency,
                        exchange_trade_id, exchange_order_id, client_order_id, meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event_ts,
                        normalize_symbol(event.symbol),
                        event.type.value,
                        event.side,
                        str(event.qty),
                        str(event.price) if event.price is not None else None,
                        str(event.fee) if event.fee is not None else None,
                        event.fee_currency,
                        event.exchange_trade_id,
                        event.exchange_order_id,
                        event.client_order_id,
                        json.dumps(event.meta, sort_keys=True),
                    ),
                )
                inserted += int(bool(cur.rowcount))
        attempted = len(events)
        return AppendResult(
            attempted=attempted, inserted=inserted, ignored=attempted - inserted
        )

    def load_ledger_events(
        self,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        symbol: str | None = None,
    ) -> list[LedgerEvent]:
        query = "SELECT * FROM ledger_events WHERE 1=1"
        params: list[str] = []
        if time_min is not None:
            query += " AND ts >= ?"
            params.append(ensure_utc(time_min).isoformat())
        if time_max is not None:
            query += " AND ts <= ?"
            params.append(ensure_utc(time_max).isoformat())
        if symbol is not None:
            query += " AND symbol = ?"
            params.append(normalize_symbol(symbol))
        query += " ORDER BY ts, event_id"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        events: list[LedgerEvent] = []
        for row in rows:
            events.append(
                LedgerEvent(
                    event_id=str(row["event_id"]),
                    ts=datetime.fromisoformat(str(row["ts"])),
                    symbol=str(row["symbol"]),
                    type=LedgerEventType(str(row["type"])),
                    side=(str(row["side"]) if row["side"] is not None else None),
                    qty=Decimal(str(row["qty"])),
                    price=(
                        Decimal(str(row["price"])) if row["price"] is not None else None
                    ),
                    fee=(Decimal(str(row["fee"])) if row["fee"] is not None else None),
                    fee_currency=(
                        str(row["fee_currency"])
                        if row["fee_currency"] is not None
                        else None
                    ),
                    exchange_trade_id=(
                        str(row["exchange_trade_id"])
                        if row["exchange_trade_id"] is not None
                        else None
                    ),
                    exchange_order_id=(
                        str(row["exchange_order_id"])
                        if row["exchange_order_id"] is not None
                        else None
                    ),
                    client_order_id=(
                        str(row["client_order_id"])
                        if row["client_order_id"] is not None
                        else None
                    ),
                    meta=json.loads(str(row["meta_json"])),
                )
            )
        return events

    def get_cursor(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM cursors WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_cursor(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cursors(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    def save_cycle_metrics(
        self,
        *,
        cycle_id: str,
        ts_start: str,
        ts_end: str,
        mode: str,
        fills_count: int,
        orders_submitted: int,
        orders_canceled: int,
        rejects_count: int,
        fill_rate: float,
        avg_time_to_fill: float | None,
        slippage_bps_avg: float | None,
        fees_json: str,
        pnl_json: str,
        meta_json: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cycle_metrics(
                    cycle_id, ts_start, ts_end, mode, fills_count, orders_submitted,
                    orders_canceled, rejects_count, fill_rate, avg_time_to_fill,
                    slippage_bps_avg, fees_json, pnl_json, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    ts_start=excluded.ts_start,
                    ts_end=excluded.ts_end,
                    mode=excluded.mode,
                    fills_count=excluded.fills_count,
                    orders_submitted=excluded.orders_submitted,
                    orders_canceled=excluded.orders_canceled,
                    rejects_count=excluded.rejects_count,
                    fill_rate=excluded.fill_rate,
                    avg_time_to_fill=excluded.avg_time_to_fill,
                    slippage_bps_avg=excluded.slippage_bps_avg,
                    fees_json=excluded.fees_json,
                    pnl_json=excluded.pnl_json,
                    meta_json=excluded.meta_json
                """,
                (
                    cycle_id,
                    ts_start,
                    ts_end,
                    mode,
                    fills_count,
                    orders_submitted,
                    orders_canceled,
                    rejects_count,
                    fill_rate,
                    avg_time_to_fill,
                    slippage_bps_avg,
                    fees_json,
                    pnl_json,
                    meta_json,
                ),
            )

    def get_risk_state_current(self) -> dict[str, str | None]:
        with self._connect() as conn:
            self._ensure_risk_budget_schema(conn)
            row = conn.execute(
                "SELECT * FROM risk_state_current WHERE state_id = 1"
            ).fetchone()
        if row is None:
            return {
                "current_mode": None,
                "peak_equity_try": None,
                "peak_equity_date": None,
                "fees_try_today": None,
                "fees_day": None,
            }
        return {
            "current_mode": (
                str(row["current_mode"]) if row["current_mode"] is not None else None
            ),
            "peak_equity_try": (
                str(row["peak_equity_try"])
                if row["peak_equity_try"] is not None
                else None
            ),
            "peak_equity_date": (
                str(row["peak_equity_date"])
                if row["peak_equity_date"] is not None
                else None
            ),
            "fees_try_today": (
                str(row["fees_try_today"])
                if row["fees_try_today"] is not None
                else None
            ),
            "fees_day": str(row["fees_day"]) if row["fees_day"] is not None else None,
        }

    def upsert_risk_state_current(
        self,
        *,
        mode: str,
        peak_equity_try: Decimal,
        peak_equity_date: str,
        fees_try_today: Decimal,
        fees_day: str,
    ) -> None:
        with self._connect() as conn:
            self._upsert_risk_state_current_with_conn(
                conn=conn,
                mode=mode,
                peak_equity_try=peak_equity_try,
                peak_equity_date=peak_equity_date,
                fees_try_today=fees_try_today,
                fees_day=fees_day,
            )

    def save_risk_decision(
        self,
        *,
        cycle_id: str,
        decision: RiskDecision,
        prev_mode: str | None,
    ) -> None:
        with self._connect() as conn:
            self._save_risk_decision_with_conn(
                conn=conn,
                cycle_id=cycle_id,
                decision=decision,
                prev_mode=prev_mode,
            )

    def persist_risk(
        self,
        *,
        cycle_id: str,
        decision: RiskDecision,
        prev_mode: str | None,
        mode: Mode,
        peak_equity_try: Decimal,
        peak_day: str,
        fees_today_try: Decimal,
        fees_day: str,
    ) -> None:
        with self.transaction() as conn:
            self._save_risk_decision_with_conn(
                conn=conn,
                cycle_id=cycle_id,
                decision=decision,
                prev_mode=prev_mode,
            )
            self._upsert_risk_state_current_with_conn(
                conn=conn,
                mode=mode.value,
                peak_equity_try=peak_equity_try,
                peak_equity_date=peak_day,
                fees_try_today=fees_today_try,
                fees_day=fees_day,
            )

    def _save_risk_decision_with_conn(
        self,
        *,
        conn: sqlite3.Connection,
        cycle_id: str,
        decision: RiskDecision,
        prev_mode: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO risk_decisions(
                decision_id, ts, mode, reasons_json, signals_json, limits_json, decision_json,
                prev_mode
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
                decision.mode.value,
                json.dumps(decision.reasons, sort_keys=True),
                self._serialize_risk_payload(decision.signals),
                self._serialize_risk_payload(decision.limits),
                self._serialize_risk_payload(decision),
                prev_mode,
            ),
        )

    def _upsert_risk_state_current_with_conn(
        self,
        *,
        conn: sqlite3.Connection,
        mode: str,
        peak_equity_try: Decimal,
        peak_equity_date: str,
        fees_try_today: Decimal,
        fees_day: str,
    ) -> None:
        conn.execute(
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
                mode,
                str(peak_equity_try),
                peak_equity_date,
                str(fees_try_today),
                fees_day,
                datetime.now(UTC).isoformat(),
            ),
        )

    def _serialize_risk_payload(self, value: object) -> str:
        from dataclasses import asdict

        from btcbot.domain.risk_budget import Mode

        def _json_default(obj: object) -> str:
            if isinstance(obj, Decimal):
                return str(obj)
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, Mode):
                return obj.value
            raise TypeError(
                f"Unsupported type for risk payload serialization: {type(obj).__name__}"
            )

        payload = asdict(value)
        return json.dumps(payload, sort_keys=True, default=_json_default)

    def save_anomaly_events(self, cycle_id: str, events: list[AnomalyEvent]) -> None:
        if not events:
            return
        with self._connect() as conn:
            self._save_anomaly_events_with_conn(
                conn=conn, cycle_id=cycle_id, events=events
            )

    def get_degrade_state_current(self) -> dict[str, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM degrade_state_current WHERE state_id = 1"
            ).fetchone()
        if row is None:
            return {}
        result: dict[str, str] = {}
        for key in (
            "cooldown_until",
            "current_override_mode",
            "last_reasons_json",
            "warn_window_count",
            "last_warn_codes_json",
            "cursor_stall_cycles_json",
            "last_reject_count",
            "updated_at",
        ):
            value = row[key]
            if value is not None:
                result[key] = str(value)
        return result

    def upsert_degrade_state_current(
        self,
        *,
        cooldown_until: str | None,
        current_override_mode: str | None,
        last_reasons_json: str,
        warn_window_count: int,
        last_warn_codes_json: str,
        cursor_stall_cycles_json: str,
        last_reject_count: int,
    ) -> None:
        with self._connect() as conn:
            self._upsert_degrade_state_current_with_conn(
                conn=conn,
                cooldown_until=cooldown_until,
                current_override_mode=current_override_mode,
                last_reasons_json=last_reasons_json,
                warn_window_count=warn_window_count,
                last_warn_codes_json=last_warn_codes_json,
                cursor_stall_cycles_json=cursor_stall_cycles_json,
                last_reject_count=last_reject_count,
            )

    def persist_degrade(
        self,
        *,
        cycle_id: str,
        events: list[AnomalyEvent],
        cooldown_until: str | None,
        current_override_mode: str | None,
        last_reasons_json: str,
        warn_window_count: int,
        last_warn_codes_json: str,
        cursor_stall_cycles_json: str,
        last_reject_count: int,
    ) -> None:
        with self.transaction() as conn:
            self._save_anomaly_events_with_conn(
                conn=conn, cycle_id=cycle_id, events=events
            )
            self._upsert_degrade_state_current_with_conn(
                conn=conn,
                cooldown_until=cooldown_until,
                current_override_mode=current_override_mode,
                last_reasons_json=last_reasons_json,
                warn_window_count=warn_window_count,
                last_warn_codes_json=last_warn_codes_json,
                cursor_stall_cycles_json=cursor_stall_cycles_json,
                last_reject_count=last_reject_count,
            )

    def _save_anomaly_events_with_conn(
        self,
        *,
        conn: sqlite3.Connection,
        cycle_id: str,
        events: list[AnomalyEvent],
    ) -> None:
        for event in events:
            event_id = f"{cycle_id}:{event.code.value}:{event.severity}"
            conn.execute(
                """
                INSERT INTO anomaly_events(id, ts, cycle_id, code, severity, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ts=excluded.ts,
                    cycle_id=excluded.cycle_id,
                    code=excluded.code,
                    severity=excluded.severity,
                    details_json=excluded.details_json
                """,
                (
                    event_id,
                    event.ts.isoformat(),
                    cycle_id,
                    event.code.value,
                    event.severity,
                    json.dumps(event.details, sort_keys=True),
                ),
            )

    def _upsert_degrade_state_current_with_conn(
        self,
        *,
        conn: sqlite3.Connection,
        cooldown_until: str | None,
        current_override_mode: str | None,
        last_reasons_json: str,
        warn_window_count: int,
        last_warn_codes_json: str,
        cursor_stall_cycles_json: str,
        last_reject_count: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO degrade_state_current(
                state_id,
                cooldown_until,
                current_override_mode,
                last_reasons_json,
                warn_window_count,
                last_warn_codes_json,
                cursor_stall_cycles_json,
                last_reject_count,
                updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(state_id) DO UPDATE SET
                cooldown_until=excluded.cooldown_until,
                current_override_mode=excluded.current_override_mode,
                last_reasons_json=excluded.last_reasons_json,
                warn_window_count=excluded.warn_window_count,
                last_warn_codes_json=excluded.last_warn_codes_json,
                cursor_stall_cycles_json=excluded.cursor_stall_cycles_json,
                last_reject_count=excluded.last_reject_count,
                updated_at=excluded.updated_at
            """,
            (
                cooldown_until,
                current_override_mode,
                last_reasons_json,
                warn_window_count,
                last_warn_codes_json,
                cursor_stall_cycles_json,
                last_reject_count,
                datetime.now(UTC).isoformat(),
            ),
        )

    def get_active_stage7_params(
        self, *, settings: object, now_utc: datetime
    ) -> Stage7Params:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version, params_json FROM stage7_params_active WHERE key = 'active'"
            ).fetchone()
            if row is not None:
                payload = json.loads(str(row["params_json"]))
                return Stage7Params.from_dict(payload)

            defaults = Stage7Params(
                universe_size=int(settings.stage7_universe_size),
                score_weights={
                    key: Decimal(str(value))
                    for key, value in (
                        settings.stage7_score_weights
                        or {
                            "liquidity": 0.5,
                            "spread": 0.3,
                            "volatility": 0.2,
                        }
                    ).items()
                },
                order_offset_bps=int(Decimal(str(settings.stage7_order_offset_bps))),
                turnover_cap_try=Decimal(str(settings.notional_cap_try_per_cycle)),
                max_orders_per_cycle=int(settings.max_orders_per_cycle),
                max_spread_bps=int(Decimal(str(settings.stage7_max_spread_bps))),
                cash_target_try=Decimal(str(settings.try_cash_target)),
                min_quote_volume_try=Decimal(str(settings.stage7_min_quote_volume_try)),
                version=1,
                updated_at=now_utc,
            )
            conn.execute(
                """
                INSERT INTO stage7_params_active(key, version, params_json, ts)
                VALUES(?, ?, ?, ?)
                """,
                (
                    "active",
                    defaults.version,
                    json.dumps(defaults.to_dict(), sort_keys=True),
                    defaults.updated_at.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO stage7_params_checkpoints(
                    version, ts, params_json, is_good
                ) VALUES (?, ?, ?, 1)
                """,
                (
                    defaults.version,
                    defaults.updated_at.isoformat(),
                    json.dumps(defaults.to_dict(), sort_keys=True),
                ),
            )
            return defaults

    def set_active_stage7_params(
        self, params: Stage7Params, change: ParamChange
    ) -> None:
        with self.transaction() as conn:
            params_json = json.dumps(params.to_dict(), sort_keys=True)
            ts_iso = params.updated_at.isoformat()
            conn.execute(
                """
                INSERT INTO stage7_params_active(key, version, params_json, ts)
                VALUES('active', ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    version=excluded.version,
                    params_json=excluded.params_json,
                    ts=excluded.ts
                """,
                (
                    params.version,
                    params_json,
                    ts_iso,
                ),
            )
            self._record_stage7_param_change_with_conn(conn=conn, change=change)
            conn.execute(
                """
                INSERT OR REPLACE INTO stage7_params_checkpoints(version, ts, params_json, is_good)
                VALUES (?, ?, ?, 1)
                """,
                (params.version, ts_iso, params_json),
            )

    def record_stage7_param_change(self, change: ParamChange) -> None:
        with self._connect() as conn:
            self._record_stage7_param_change_with_conn(conn=conn, change=change)

    def _record_stage7_param_change_with_conn(
        self,
        *,
        conn: sqlite3.Connection,
        change: ParamChange,
    ) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO stage7_param_changes(
                change_id, ts, from_version, to_version,
                change_json, outcome, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change.change_id,
                ensure_utc(change.ts).isoformat(),
                change.from_version,
                change.to_version,
                json.dumps(change.to_dict(), sort_keys=True),
                change.outcome,
                change.reason,
            ),
        )

    def set_stage7_checkpoint_goodness(self, version: int, is_good: bool) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE stage7_params_checkpoints
                SET is_good = ?
                WHERE version = ?
                """,
                (1 if is_good else 0, version),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO stage7_params_checkpoints(
                        version, ts, params_json, is_good
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (version, datetime.now(UTC).isoformat(), "{}", 1 if is_good else 0),
                )

    def update_stage7_cycle_adaptation_metadata(
        self,
        *,
        cycle_id: str,
        active_param_version: int,
        param_change: ParamChange | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE stage7_cycle_trace
                SET active_param_version = ?, param_change_json = ?
                WHERE cycle_id = ?
                """,
                (
                    int(active_param_version),
                    (
                        json.dumps(param_change.to_dict(), sort_keys=True)
                        if param_change is not None
                        else "{}"
                    ),
                    cycle_id,
                ),
            )

    def get_last_good_stage7_params_checkpoint(self) -> Stage7Params | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT params_json
                FROM stage7_params_checkpoints
                WHERE is_good = 1
                ORDER BY version DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return Stage7Params.from_dict(json.loads(str(row["params_json"])))

    def get_previous_good_stage7_params_checkpoint(
        self, *, before_version: int
    ) -> Stage7Params | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT params_json
                FROM stage7_params_checkpoints
                WHERE is_good = 1 AND version < ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (before_version,),
            ).fetchone()
        if row is None:
            return None
        return Stage7Params.from_dict(json.loads(str(row["params_json"])))

    def persist_agent_decision_audit(
        self,
        *,
        cycle_id: str,
        correlation_id: str,
        context_json: str,
        decision_json: str,
        safe_decision_json: str,
        diff_json: str,
        diff_hash: str,
        prompt_json: str | None,
        response_json: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_decision_audit (
                    cycle_id,
                    correlation_id,
                    context_json,
                    decision_json,
                    safe_decision_json,
                    diff_json,
                    diff_hash,
                    prompt_json,
                    response_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, correlation_id)
                DO UPDATE SET
                    context_json=excluded.context_json,
                    decision_json=excluded.decision_json,
                    safe_decision_json=excluded.safe_decision_json,
                    diff_json=excluded.diff_json,
                    diff_hash=excluded.diff_hash,
                    prompt_json=excluded.prompt_json,
                    response_json=excluded.response_json,
                    ts=CURRENT_TIMESTAMP
                """,
                (
                    cycle_id,
                    correlation_id,
                    context_json,
                    decision_json,
                    safe_decision_json,
                    diff_json,
                    diff_hash,
                    prompt_json,
                    response_json,
                ),
            )
