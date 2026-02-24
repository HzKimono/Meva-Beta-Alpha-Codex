from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.accounting import Position, TradeFill
from btcbot.domain.intent import Intent
from btcbot.domain.models import OrderSide
from btcbot.services.state_store import StateStore


def test_stage3_tables_created(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    with store._connect() as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"fills", "positions", "intents"}.issubset(names)


def test_fill_and_position_persistence(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    fill = TradeFill(
        fill_id="f1",
        order_id="o1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=Decimal("100"),
        qty=Decimal("1"),
        fee=Decimal("0.1"),
        fee_currency="TRY",
        ts=datetime.now(UTC),
    )
    assert store.save_fill(fill) is True
    assert store.save_fill(fill) is False

    pos = Position(
        symbol="BTCTRY",
        qty=Decimal("1"),
        avg_cost=Decimal("100.1"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("2"),
        fees_paid=Decimal("0.1"),
        updated_at=datetime.now(UTC),
    )
    store.save_position(pos)
    loaded = store.get_position("BTC_TRY")
    assert loaded is not None
    assert loaded.avg_cost == Decimal("100.1")


def test_record_intent_and_last_ts(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    intent = Intent.create(
        cycle_id="c1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        qty=Decimal("1"),
        limit_price=Decimal("100"),
        reason="test",
    )
    store.record_intent(intent, ts)
    data = store.get_last_intent_ts_by_symbol_side()
    assert data[("BTCTRY", "buy")] == ts


def test_state_store_init_is_idempotent(tmp_path) -> None:
    db_path = str(tmp_path / "state.db")
    StateStore(db_path=db_path)
    StateStore(db_path=db_path)


def test_stage7_schema_init_twice_is_idempotent(tmp_path) -> None:
    db_path = str(tmp_path / "state_stage7.db")
    first = StateStore(db_path=db_path)
    second = StateStore(db_path=db_path)

    with first._connect() as conn:
        names_first = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    with second._connect() as conn:
        names_second = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    required = {
        "stage7_cycle_trace",
        "stage7_ledger_metrics",
        "stage7_run_metrics",
        "stage7_param_changes",
        "stage7_params_checkpoints",
        "stage7_params_active",
    }
    assert required.issubset(names_first)
    assert names_first == names_second


def test_attach_action_metadata_persists_intent_identity(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    action_id = store.record_action("c1", "would_place_order", "hash-1")
    assert action_id is not None

    store.attach_action_metadata(
        action_id=action_id,
        client_order_id="cid-1",
        order_id="oid-1",
        reconciled=False,
        reconcile_status=None,
        reconcile_reason=None,
        idempotency_key="idem-1",
        intent_id="intent-1",
    )

    row = store.get_action_by_id(action_id)
    assert row is not None
    import json

    payload = json.loads(str(row["metadata_json"]))
    assert payload["idempotency_key"] == "idem-1"
    assert payload["intent_id"] == "intent-1"


def test_state_store_concurrent_open_close_does_not_lock(tmp_path) -> None:
    def _worker(worker_id: int) -> None:
        for i in range(15):
            db_path = str(tmp_path / f"concurrent_state_{worker_id}_{i}.db")
            store = StateStore(db_path=db_path)
            store.record_action(f"c-{worker_id}-{i}", "noop", f"hash-{worker_id}-{i}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_worker, worker_id) for worker_id in range(4)]
        for future in futures:
            future.result()
