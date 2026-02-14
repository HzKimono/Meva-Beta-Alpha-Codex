from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.domain.models import Order, OrderSide, OrderStatus
from btcbot.domain.risk_models import RiskDecision, RiskMode
from btcbot.services import state_store as state_store_module
from btcbot.services.state_store import StateStore


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


def test_record_action_returns_action_id_and_dedupes(tmp_path) -> None:
    db_path = str(tmp_path / "state.db")
    first = StateStore(db_path=db_path)
    action_id = first.record_action("c1", "sweep_plan", "hash-1", dedupe_window_seconds=3600)
    assert action_id is not None

    second = StateStore(db_path=db_path)
    assert second.record_action("c2", "sweep_plan", "hash-1", dedupe_window_seconds=3600) is None
    assert second.action_count("sweep_plan", "hash-1") == 1


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

    monkeypatch.setattr(state_store_module, "datetime", StepDateTime)

    store = StateStore(db_path=str(tmp_path / "state.db"))
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
    assert row[0] == json.dumps({"z": 2, "a": 1}, sort_keys=True)
