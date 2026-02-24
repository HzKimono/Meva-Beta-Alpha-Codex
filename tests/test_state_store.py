from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.domain.models import Order, OrderSide, OrderStatus
from btcbot.domain.order_state import OrderStatus as Stage7OrderStatus
from btcbot.domain.order_state import Stage7Order
from btcbot.domain.risk_budget import Mode, RiskLimits, RiskSignals
from btcbot.domain.risk_budget import RiskDecision as BudgetRiskDecision
from btcbot.domain.risk_models import RiskDecision, RiskMode
from btcbot.services import state_store as state_store_module
from btcbot.services.parity import compute_run_fingerprint
from btcbot.services.state_store import IdempotencyConflictError, StateStore


def test_risk_state_current_table_is_created_on_fresh_db(tmp_path) -> None:
    db_path = tmp_path / "fresh.sqlite"
    db = StateStore(str(db_path))

    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_state_current'"
        ).fetchone()

    assert row is not None
    assert db.get_risk_state_current() == {
        "current_mode": None,
        "peak_equity_try": None,
        "peak_equity_date": None,
        "fees_try_today": None,
        "fees_day": None,
    }


def test_state_store_mode_persistence(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    risk_decision = BudgetRiskDecision(
        mode=Mode.REDUCE_RISK_ONLY,
        reasons=["test"],
        limits=RiskLimits(
            max_daily_drawdown_try=Decimal("100"),
            max_drawdown_try=Decimal("200"),
            max_gross_exposure_try=Decimal("300"),
            max_position_pct=Decimal("0.25"),
            max_order_notional_try=Decimal("50"),
        ),
        signals=RiskSignals(
            equity_try=Decimal("1000"),
            peak_equity_try=Decimal("1100"),
            drawdown_try=Decimal("100"),
            daily_pnl_try=Decimal("-10"),
            gross_exposure_try=Decimal("250"),
            largest_position_pct=Decimal("0.1"),
            fees_try_today=Decimal("1"),
        ),
        decided_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    store.persist_risk(
        cycle_id="c1",
        decision=risk_decision,
        prev_mode=Mode.NORMAL,
        risk_mode=Mode.REDUCE_RISK_ONLY,
        peak_equity_try=Decimal("1100"),
        peak_day="2024-01-01",
        fees_today_try=Decimal("1"),
        fees_day="2024-01-01",
    )

    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        row = conn.execute(
            "SELECT mode, prev_mode FROM risk_decisions WHERE decision_id='c1'"
        ).fetchone()
        current = conn.execute(
            "SELECT current_mode FROM risk_state_current WHERE state_id=1"
        ).fetchone()

    assert row == ("REDUCE_RISK_ONLY", "NORMAL")
    assert current == ("REDUCE_RISK_ONLY",)

    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        conn.execute("UPDATE risk_decisions SET mode='normal' WHERE decision_id='c1'")

    assert store.get_latest_risk_mode() == Mode.NORMAL


def test_orders_unknown_retry_columns_are_added_for_legacy_db(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE orders (
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

    StateStore(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}

    assert "unknown_first_seen_at" in columns
    assert "unknown_last_probe_at" in columns
    assert "unknown_next_probe_at" in columns
    assert "unknown_probe_attempts" in columns
    assert "unknown_escalated_at" in columns


def test_record_action_returns_action_id_and_dedupes(tmp_path) -> None:
    db_path = str(tmp_path / "state.db")
    first = StateStore(db_path=db_path)
    action_id = first.record_action("c1", "sweep_plan", "hash-1", dedupe_window_seconds=3600)
    assert action_id is not None

    second = StateStore(db_path=db_path)
    assert second.record_action("c2", "sweep_plan", "hash-1", dedupe_window_seconds=3600) is None
    assert second.action_count("sweep_plan", "hash-1") == 1


def test_state_store_strict_instance_lock_fails_on_active_conflict(tmp_path) -> None:
    db_path = str(tmp_path / "strict.db")
    store = StateStore(db_path=db_path)
    now_epoch = int(datetime.now(UTC).timestamp())
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO process_instances(instance_id, pid, db_path, started_at_epoch, heartbeat_at_epoch)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "99999-conflict",
                99999,
                store.db_path_abs,
                now_epoch,
                now_epoch,
            ),
        )

    with pytest.raises(RuntimeError, match="STATE_DB_LOCK_CONFLICT"):
        StateStore(db_path=db_path, strict_instance_lock=True)


def test_reserve_idempotency_payload_mismatch_raises(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    first = store.reserve_idempotency_key("place_order", "k-1", "payload-a", ttl_seconds=60)
    assert first.reserved is True

    with pytest.raises(IdempotencyConflictError):
        store.reserve_idempotency_key("place_order", "k-1", "payload-b", ttl_seconds=60)


def test_reserve_idempotency_defaults_recovery_tracking_fields(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    reservation = store.reserve_idempotency_key(
        "cancel_order", "cancel:r1", "hash-1", ttl_seconds=60
    )

    assert reservation.recovery_attempts == 0
    assert reservation.next_recovery_at_epoch is None


def test_reserve_idempotency_existing_key_returns_not_reserved(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    first = store.reserve_idempotency_key("cancel_order", "cancel:o1", "hash-1", ttl_seconds=60)
    second = store.reserve_idempotency_key("cancel_order", "cancel:o1", "hash-1", ttl_seconds=60)

    assert first.reserved is True
    assert second.reserved is False
    assert second.status == "PENDING"


def test_reserve_idempotency_stale_pending_without_metadata_is_recovered(
    monkeypatch, tmp_path
) -> None:
    class _T0:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    class _T1:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 0, 2, 0, tzinfo=UTC)

    monkeypatch.setattr(state_store_module, "datetime", _T0)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    first = store.reserve_idempotency_key("cancel_order", "cancel:stale", "hash-1", ttl_seconds=60)
    assert first.reserved is True

    monkeypatch.setattr(state_store_module, "datetime", _T1)
    second = store.reserve_idempotency_key("cancel_order", "cancel:stale", "hash-1", ttl_seconds=60)

    assert second.reserved is True
    assert second.status == "PENDING"


def test_reserve_idempotency_can_promote_simulated(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    first = store.reserve_idempotency_key("place_order", "k-sim", "payload-a", ttl_seconds=60)
    store.finalize_idempotency_key(
        "place_order",
        "k-sim",
        action_id=first.action_id,
        client_order_id=None,
        order_id=None,
        status="SIMULATED",
    )

    blocked = store.reserve_idempotency_key("place_order", "k-sim", "payload-a", ttl_seconds=60)
    promoted = store.reserve_idempotency_key(
        "place_order",
        "k-sim",
        "payload-a",
        ttl_seconds=60,
        allow_promote_simulated=True,
    )

    assert blocked.reserved is False
    assert blocked.status == "SIMULATED"
    assert promoted.reserved is True
    assert promoted.status == "PENDING"


def test_reserve_idempotency_failed_row_can_be_retried(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    first = store.reserve_idempotency_key(
        "cancel_order", "cancel:o-fail", "payload-a", ttl_seconds=60
    )
    store.finalize_idempotency_key(
        "cancel_order",
        "cancel:o-fail",
        action_id=first.action_id,
        client_order_id="cid",
        order_id="oid",
        status="FAILED",
    )

    retry = store.reserve_idempotency_key(
        "cancel_order", "cancel:o-fail", "payload-a", ttl_seconds=60
    )
    assert retry.reserved is True
    assert retry.status == "PENDING"


def test_attach_action_metadata_updates_exact_row(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))

    first_id = store.record_action("c1", "place_order", "same")
    second_id = store.record_action("c2", "place_order", "same-2", dedupe_window_seconds=0)
    assert first_id is not None
    assert second_id is not None

    store.attach_action_metadata(
        action_id=first_id,
        client_order_id="coid-first",
        order_id="oid-first",
        reconciled=True,
        reconcile_status="confirmed",
        reconcile_reason="matched-first",
    )

    row1 = store.get_action_by_id(first_id)
    row2 = store.get_action_by_id(second_id)
    assert row1 is not None and row2 is not None
    assert row1["order_id"] == "oid-first"
    assert row2["order_id"] is None

    metadata = json.loads(row1["metadata_json"])
    assert metadata["reconciled"] is True


def test_save_order_persists_exact_price_qty_strings(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    order = Order(
        order_id="oid-1",
        client_order_id="cid-1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=123.456789123,
        quantity=0.123456789,
        status=OrderStatus.NEW,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    store.save_order(order)
    stored = store.find_open_or_unknown_orders(["BTCTRY"])[0]
    assert str(stored.price) == "123.456789123"
    assert str(stored.quantity) == "0.123456789"


def test_update_order_status_unknown_fields_set_and_cleared(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    order = Order(
        order_id="oid-unknown-fields",
        client_order_id="cid-unknown-fields",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    store.save_order(order)

    store.update_order_status(order_id=order.order_id, status=OrderStatus.UNKNOWN)

    with store._connect() as conn:
        unknown_row = conn.execute(
            """
            SELECT unknown_first_seen_at, unknown_last_probe_at, unknown_next_probe_at,
                   unknown_probe_attempts, unknown_escalated_at
            FROM orders
            WHERE order_id = ?
            """,
            (order.order_id,),
        ).fetchone()

    assert unknown_row is not None
    assert unknown_row["unknown_first_seen_at"] is not None
    assert unknown_row["unknown_next_probe_at"] is not None
    assert unknown_row["unknown_probe_attempts"] == 0
    assert unknown_row["unknown_last_probe_at"] is None
    assert unknown_row["unknown_escalated_at"] is None

    store.update_order_status(order_id=order.order_id, status=OrderStatus.OPEN)

    with store._connect() as conn:
        open_row = conn.execute(
            """
            SELECT unknown_first_seen_at, unknown_last_probe_at, unknown_next_probe_at,
                   unknown_probe_attempts, unknown_escalated_at
            FROM orders
            WHERE order_id = ?
            """,
            (order.order_id,),
        ).fetchone()

    assert open_row is not None
    assert open_row["unknown_first_seen_at"] is None
    assert open_row["unknown_last_probe_at"] is None
    assert open_row["unknown_next_probe_at"] is None
    assert open_row["unknown_probe_attempts"] == 0
    assert open_row["unknown_escalated_at"] is None


def test_update_order_status_marks_reconciled_for_terminal_exchange_status(
    monkeypatch, tmp_path
) -> None:
    class FixedDateTime:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(state_store_module, "datetime", FixedDateTime)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    order = Order(
        order_id="oid-terminal",
        client_order_id="cid-terminal",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
    )
    store.save_order(order)

    store.update_order_status(
        order_id="oid-terminal",
        status=OrderStatus.CANCELED,
        exchange_status_raw="Canceled",
        reconciled=True,
        last_seen_at=1_700_000_000_100,
    )

    with store._connect() as conn:
        row = conn.execute(
            """
            SELECT status, exchange_status_raw, reconciled, updated_at, last_seen_at
            FROM orders
            WHERE order_id = ?
            """,
            ("oid-terminal",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "canceled"
    assert row["exchange_status_raw"] == "Canceled"
    assert row["reconciled"] == 1
    assert row["updated_at"] == "2024-01-01T12:00:00+00:00"
    assert row["last_seen_at"] == 1_700_000_000_100


def test_update_order_status_marks_reconciled_for_open_exchange_observation(
    monkeypatch, tmp_path
) -> None:
    class FixedDateTime:
        @staticmethod
        def now(tz):
            del tz
            return datetime(2024, 1, 1, 12, 5, tzinfo=UTC)

    monkeypatch.setattr(state_store_module, "datetime", FixedDateTime)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    order = Order(
        order_id="oid-open",
        client_order_id="cid-open",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=100.0,
        quantity=0.1,
        status=OrderStatus.OPEN,
        created_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
    )
    store.save_order(order)

    store.update_order_status(
        order_id="oid-open",
        status=OrderStatus.OPEN,
        exchange_status_raw="Open",
        reconciled=True,
        last_seen_at=1_700_000_000_200,
    )

    with store._connect() as conn:
        row = conn.execute(
            """
            SELECT status, exchange_status_raw, reconciled, updated_at, last_seen_at
            FROM orders
            WHERE order_id = ?
            """,
            ("oid-open",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "open"
    assert row["exchange_status_raw"] == "Open"
    assert row["reconciled"] == 1
    assert row["updated_at"] == "2024-01-01T12:05:00+00:00"
    assert row["last_seen_at"] == 1_700_000_000_200


def test_state_store_sets_sqlite_busy_timeout_and_wal(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))

    with store._connect() as conn:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert int(busy_timeout) >= 5000
    assert str(journal_mode).lower() == "wal"


def test_connect_context_manager_commits_and_closes(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def execute(self, _query: str):
            return None

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    fake_conn = FakeConnection()
    monkeypatch.setattr(state_store_module.sqlite3, "connect", lambda *args, **kwargs: fake_conn)

    store = object.__new__(StateStore)
    store.db_path = "fake.db"

    with store._connect() as conn:
        assert conn is fake_conn

    assert fake_conn.committed is True
    assert fake_conn.rolled_back is False
    assert fake_conn.closed is True


def test_connect_context_manager_rolls_back_and_closes_on_error(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def execute(self, _query: str):
            return None

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    fake_conn = FakeConnection()
    monkeypatch.setattr(state_store_module.sqlite3, "connect", lambda *args, **kwargs: fake_conn)

    store = object.__new__(StateStore)
    store.db_path = "fake.db"

    try:
        with store._connect():
            raise RuntimeError("boom")
    except RuntimeError as exc:
        assert str(exc) == "boom"

    assert fake_conn.committed is False
    assert fake_conn.rolled_back is True
    assert fake_conn.closed is True


def test_record_action_dedupes_in_same_bucket(monkeypatch, tmp_path) -> None:
    class FixedDateTime:
        @staticmethod
        def now(tz):
            del tz
            return datetime.fromtimestamp(1_000, UTC)

    monkeypatch.setattr(state_store_module, "datetime", FixedDateTime)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    first = store.record_action("c1", "place_order", "hash-1", dedupe_window_seconds=300)
    second = store.record_action("c2", "place_order", "hash-1", dedupe_window_seconds=300)

    assert first is not None
    assert second is None
    assert store.action_count("place_order", "hash-1") == 1


def test_record_action_allows_same_payload_in_different_bucket(monkeypatch, tmp_path) -> None:
    class StepDateTime:
        now_epoch = 1_000

        @classmethod
        def now(cls, tz):
            del tz
            value = cls.now_epoch
            cls.now_epoch = 1_400
            return datetime.fromtimestamp(value, UTC)

    store = StateStore(db_path=str(tmp_path / "state.db"))
    monkeypatch.setattr(state_store_module, "datetime", StepDateTime)
    first = store.record_action("c1", "place_order", "hash-2", dedupe_window_seconds=300)
    second = store.record_action("c2", "place_order", "hash-2", dedupe_window_seconds=300)

    assert first is not None
    assert second is not None
    assert store.action_count("place_order", "hash-2") == 2


def test_connect_close_failure_does_not_mask_primary_error(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.committed = False
            self.rolled_back = False

        def execute(self, _query: str):
            return None

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            raise RuntimeError("close boom")

    fake_conn = FakeConnection()
    monkeypatch.setattr(state_store_module.sqlite3, "connect", lambda *args, **kwargs: fake_conn)

    store = object.__new__(StateStore)
    store.db_path = "fake.db"

    with pytest.raises(RuntimeError, match="primary boom"):
        with store._connect():
            raise RuntimeError("primary boom")

    assert fake_conn.committed is False
    assert fake_conn.rolled_back is True


def test_stage4_open_orders_excludes_external_by_default(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    store.record_stage4_order_submitted(
        symbol="BTC_TRY",
        client_order_id="cid-live",
        exchange_order_id="ex-live",
        side="buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        mode="live",
    )

    class ExternalOrder:
        symbol = "BTC_TRY"
        side = "sell"
        price = Decimal("101")
        qty = Decimal("1")
        status = "open"
        exchange_order_id = "ex-ext"
        client_order_id = "cid-ext"

    store.import_stage4_external_order(ExternalOrder())

    visible_default = store.list_stage4_open_orders()
    visible_with_external = store.list_stage4_open_orders(include_external=True)

    assert {o.client_order_id for o in visible_default} == {"cid-live"}
    assert {o.client_order_id for o in visible_with_external} == {"cid-live", "cid-ext"}


def test_record_stage4_order_error_persists_context(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    store.record_stage4_order_error(
        client_order_id="cid-err",
        reason="cancel_missing_exchange_order_id",
        symbol="BTC_TRY",
        side="sell",
        price=Decimal("500"),
        qty=Decimal("0.1"),
        mode="live",
        status="error",
    )

    order = store.get_stage4_order_by_client_id("cid-err")
    assert order is not None
    assert order.symbol == "BTCTRY"
    assert order.side == "sell"
    assert order.price == Decimal("500")
    assert order.qty == Decimal("0.1")

    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, mode, last_error FROM stage4_orders WHERE client_order_id = ?",
            ("cid-err",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "error"
    assert row["mode"] == "live"
    assert row["last_error"] == "cancel_missing_exchange_order_id"


def test_stage7_risk_decision_saved_and_latest_fetchable(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "stage7_risk.db"))
    decision = RiskDecision(
        mode=RiskMode.REDUCE_RISK_ONLY,
        reasons={"a": 1, "z": ["x"]},
        cooldown_until=None,
        decided_at=datetime(2024, 1, 1, tzinfo=UTC),
        inputs_hash="abc",
    )

    store.save_stage7_risk_decision(cycle_id="c1", decision=decision)
    latest = store.get_latest_stage7_risk_decision()

    assert latest is not None
    assert latest.mode == RiskMode.REDUCE_RISK_ONLY
    assert latest.reasons == {"a": 1, "z": ["x"]}
    assert latest.inputs_hash == "abc"


def test_stage7_risk_reasons_json_is_stable_sorted(tmp_path) -> None:
    db_path = tmp_path / "stage7_risk_sorted.db"
    store = StateStore(db_path=str(db_path))
    decision = RiskDecision(
        mode=RiskMode.OBSERVE_ONLY,
        reasons={"z": 2, "a": 1},
        cooldown_until=None,
        decided_at=datetime(2024, 1, 1, tzinfo=UTC),
        inputs_hash="hash",
    )
    store.save_stage7_risk_decision(cycle_id="c2", decision=decision)

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT reasons_json FROM stage7_risk_decisions LIMIT 1").fetchone()

    assert row is not None
    assert json.loads(row[0]) == {"a": 1, "z": 2}


def test_stage7_upsert_orders_conflict_on_client_order_id_keeps_original_order_id(
    tmp_path,
) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    now = datetime(2024, 1, 1, tzinfo=UTC)

    first = Stage7Order(
        order_id="order-A",
        client_order_id="client-X",
        cycle_id="cycle-1",
        symbol="BTCTRY",
        side="BUY",
        order_type="LIMIT",
        price_try=Decimal("100"),
        qty=Decimal("1"),
        filled_qty=Decimal("0"),
        avg_fill_price_try=None,
        status=Stage7OrderStatus.PLANNED,
        last_update=now,
        intent_hash="hash-a",
    )
    second = Stage7Order(
        order_id="order-B",
        client_order_id="client-X",
        cycle_id="cycle-2",
        symbol="BTCTRY",
        side="BUY",
        order_type="LIMIT",
        price_try=Decimal("101"),
        qty=Decimal("2"),
        filled_qty=Decimal("1"),
        avg_fill_price_try=Decimal("101"),
        status=Stage7OrderStatus.ACKED,
        last_update=now,
        intent_hash="hash-b",
    )

    store.upsert_stage7_orders([first])
    store.upsert_stage7_orders([second])

    with store._connect() as conn:
        row_count = conn.execute("SELECT COUNT(*) FROM stage7_orders").fetchone()[0]
        row = conn.execute(
            """
            SELECT order_id, client_order_id, cycle_id, qty, status
            FROM stage7_orders
            WHERE client_order_id = ?
            """,
            ("client-X",),
        ).fetchone()

    assert row_count == 1
    assert row is not None
    assert row["order_id"] == "order-A"
    assert row["client_order_id"] == "client-X"
    assert row["cycle_id"] == "cycle-2"
    assert row["qty"] == "2"
    assert row["status"] == Stage7OrderStatus.ACKED.value


def test_stage7_idempotency_key_contract(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))

    assert store.try_register_idempotency_key("k1", "h1") is True
    assert store.try_register_idempotency_key("k1", "h1") is False
    with pytest.raises(IdempotencyConflictError):
        store.try_register_idempotency_key("k1", "h2")


def test_stage7_schema_upgrade_from_legacy_snapshot(tmp_path) -> None:
    db_path = tmp_path / "legacy_stage7.sqlite"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE stage7_cycle_trace (
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
        conn.execute(
            """
            CREATE TABLE stage7_run_metrics (
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
                events_appended INTEGER NOT NULL,
                events_ignored INTEGER NOT NULL,
                equity_try TEXT NOT NULL,
                gross_pnl_try TEXT NOT NULL,
                net_pnl_try TEXT NOT NULL,
                fees_try TEXT NOT NULL,
                slippage_try TEXT NOT NULL,
                max_drawdown_pct TEXT NOT NULL,
                turnover_try TEXT NOT NULL,
                latency_ms_total INTEGER NOT NULL,
                selection_ms INTEGER NOT NULL,
                planning_ms INTEGER NOT NULL,
                intents_ms INTEGER NOT NULL,
                oms_ms INTEGER NOT NULL,
                ledger_ms INTEGER NOT NULL,
                persist_ms INTEGER NOT NULL,
                quality_flags_json TEXT NOT NULL,
                alert_flags_json TEXT NOT NULL
            )
            """
        )

    store = StateStore(db_path=str(db_path))

    ts = datetime(2024, 1, 1, tzinfo=UTC)
    store.save_stage7_cycle(
        cycle_id="legacy-upgrade-1",
        ts=ts,
        selected_universe=["BTCTRY"],
        universe_scores=[],
        intents_summary={"rules_stats": {}},
        mode_payload={"final_mode": "OBSERVE_ONLY"},
        order_decisions=[{"status": "skipped", "reason": "observe_only"}],
        portfolio_plan={},
        ledger_metrics={
            "gross_pnl_try": Decimal("0"),
            "realized_pnl_try": Decimal("0"),
            "unrealized_pnl_try": Decimal("0"),
            "net_pnl_try": Decimal("0"),
            "fees_try": Decimal("0"),
            "slippage_try": Decimal("0"),
            "turnover_try": Decimal("0"),
            "equity_try": Decimal("1000"),
            "max_drawdown": Decimal("0"),
        },
        run_metrics={
            "ts": ts.isoformat(),
            "mode_base": "NORMAL",
            "mode_final": "OBSERVE_ONLY",
            "universe_size": 1,
            "intents_planned_count": 0,
            "intents_skipped_count": 0,
            "oms_submitted_count": 0,
            "oms_filled_count": 0,
            "oms_rejected_count": 0,
            "oms_canceled_count": 0,
            "events_appended": 0,
            "events_ignored": 0,
            "equity_try": Decimal("1000"),
            "gross_pnl_try": Decimal("0"),
            "net_pnl_try": Decimal("0"),
            "fees_try": Decimal("0"),
            "slippage_try": Decimal("0"),
            "max_drawdown_pct": Decimal("0"),
            "turnover_try": Decimal("0"),
            "latency_ms_total": 1,
            "selection_ms": 1,
            "planning_ms": 1,
            "intents_ms": 1,
            "oms_ms": 1,
            "ledger_ms": 1,
            "persist_ms": 1,
            "quality_flags": {},
            "alert_flags": {},
            "run_id": "legacy-run",
        },
    )

    with store._connect() as conn:
        upgraded = conn.execute("PRAGMA table_info(stage7_cycle_trace)").fetchall()
        names = {row["name"] for row in upgraded}
        assert "active_param_version" in names
        assert "param_change_json" in names
        row = conn.execute(
            "SELECT cycle_id, active_param_version, param_change_json FROM stage7_cycle_trace"
        ).fetchone()
        assert row is not None
        assert row["cycle_id"] == "legacy-upgrade-1"


def test_save_stage7_cycle_error_includes_substep_context(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "diag.sqlite"))
    ts = datetime(2024, 1, 1, tzinfo=UTC)

    with pytest.raises(RuntimeError) as exc_info:
        store.save_stage7_cycle(
            cycle_id="diag-cycle",
            ts=ts,
            selected_universe=["BTCTRY"],
            universe_scores=[],
            intents_summary={"rules_stats": {}},
            mode_payload={"final_mode": "NORMAL"},
            order_decisions=[],
            portfolio_plan={},
            ledger_metrics={
                "gross_pnl_try": Decimal("0"),
                "realized_pnl_try": Decimal("0"),
                "unrealized_pnl_try": Decimal("0"),
                "net_pnl_try": Decimal("0"),
                "fees_try": Decimal("0"),
                "slippage_try": Decimal("0"),
                "turnover_try": Decimal("0"),
                "equity_try": Decimal("1000"),
                "max_drawdown": Decimal("0"),
            },
            run_metrics={"run_id": "run-1"},
        )

    msg = str(exc_info.value)
    assert "save_stage7_cycle failed at run_metrics_upsert" in msg
    assert "cycle_id=diag-cycle run_id=run-1" in msg


def test_stage7_schema_upgrade_from_minimal_legacy_db_supports_parity(tmp_path) -> None:
    db_path = tmp_path / "legacy_minimal.sqlite"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")

    store = StateStore(db_path=str(db_path))
    ts = datetime(2024, 1, 2, tzinfo=UTC)

    store.save_stage7_cycle(
        cycle_id="legacy-minimal-1",
        ts=ts,
        selected_universe=["BTCTRY"],
        universe_scores=[],
        intents_summary={"rules_stats": {}},
        mode_payload={"base_mode": "NORMAL", "final_mode": "OBSERVE_ONLY"},
        order_decisions=[],
        portfolio_plan={},
        ledger_metrics={
            "gross_pnl_try": Decimal("0"),
            "realized_pnl_try": Decimal("0"),
            "unrealized_pnl_try": Decimal("0"),
            "net_pnl_try": Decimal("0"),
            "fees_try": Decimal("0"),
            "slippage_try": Decimal("0"),
            "turnover_try": Decimal("0"),
            "equity_try": Decimal("1000"),
            "max_drawdown": Decimal("0"),
        },
        run_metrics={
            "ts": ts.isoformat(),
            "mode_base": "NORMAL",
            "mode_final": "OBSERVE_ONLY",
            "universe_size": 1,
            "intents_planned_count": 0,
            "intents_skipped_count": 0,
            "oms_submitted_count": 0,
            "oms_filled_count": 0,
            "oms_rejected_count": 0,
            "oms_canceled_count": 0,
            "events_appended": 0,
            "events_ignored": 0,
            "equity_try": Decimal("1000"),
            "gross_pnl_try": Decimal("0"),
            "net_pnl_try": Decimal("0"),
            "fees_try": Decimal("0"),
            "slippage_try": Decimal("0"),
            "max_drawdown_pct": Decimal("0"),
            "turnover_try": Decimal("0"),
            "latency_ms_total": 1,
            "selection_ms": 1,
            "planning_ms": 1,
            "intents_ms": 1,
            "oms_ms": 1,
            "ledger_ms": 1,
            "persist_ms": 1,
            "quality_flags": {},
            "alert_flags": {},
            "run_id": "legacy-minimal-run",
        },
    )

    exports = store.fetch_stage7_cycles_for_export(limit=5)
    assert len(exports) == 1
    assert exports[0]["cycle_id"] == "legacy-minimal-1"

    fingerprint = compute_run_fingerprint(
        db_path,
        from_ts=datetime(2024, 1, 1, tzinfo=UTC),
        to_ts=datetime(2024, 1, 3, tzinfo=UTC),
    )
    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64


def test_reject1123_rolling_window_reset(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    first = store.record_symbol_reject("btc_try", 1123, 1000)
    second = store.record_symbol_reject("btc_try", 1123, 1020)

    assert first["rolling_count"] == 1
    assert second["rolling_count"] == 2
    assert second["cooldown_active"] is False

    rolled = store.record_symbol_reject("btc_try", 1123, 1000 + 61 * 60)
    assert rolled["rolling_count"] == 1
    assert rolled["window_start_ts"] == 1000 + 61 * 60


def test_reject1123_threshold_triggers_and_extends_cooldown(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    store.record_symbol_reject("eth_try", 1123, 2000, threshold=3, cooldown_minutes=10)
    store.record_symbol_reject("eth_try", 1123, 2010, threshold=3, cooldown_minutes=10)
    triggered = store.record_symbol_reject("eth_try", 1123, 2020, threshold=3, cooldown_minutes=10)

    assert triggered["cooldown_active"] is True
    assert triggered["cooldown_until_ts"] == 2020 + 600

    extended = store.record_symbol_reject("eth_try", 1123, 2050, threshold=3, cooldown_minutes=10)
    assert extended["cooldown_until_ts"] == 2050 + 600

    active = store.list_active_cooldowns(2051)
    assert "ETHTRY" in active
