from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.order_state import OrderStatus, Stage7Order, make_intent_hash, make_order_id
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.state_store import StateStore


def test_reconcile_resumes_without_duplicates(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    now = datetime(2024, 1, 1, tzinfo=UTC)
    cid = "s7:c1:BTCTRY:BUY:abc000000001"

    partial_order = Stage7Order(
        order_id=make_order_id(cid),
        client_order_id=cid,
        cycle_id="cycle-1",
        symbol="BTCTRY",
        side="BUY",
        order_type="LIMIT",
        price_try=Decimal("100"),
        qty=Decimal("1"),
        filled_qty=Decimal("0"),
        avg_fill_price_try=None,
        status=OrderStatus.SUBMITTED,
        last_update=now,
        intent_hash=make_intent_hash(
            {
                "cycle_id": "cycle-1",
                "symbol": "BTCTRY",
                "side": "BUY",
                "order_type": "LIMIT",
                "price_try": "100",
                "qty": "1",
                "notional_try": "100",
                "client_order_id": cid,
            }
        ),
    )
    store.upsert_stage7_orders([partial_order])

    oms = OMSService()
    orders, events = oms.reconcile_open_orders(
        cycle_id="cycle-2",
        now_utc=now,
        state_store=store,
        settings=settings,
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
    )

    assert orders
    assert orders[0].status in {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED}

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT event_id, event_type FROM stage7_order_events WHERE client_order_id = ?",
            (cid,),
        ).fetchall()
    event_ids = [str(row["event_id"]) for row in rows]
    event_types = [str(row["event_type"]) for row in rows]

    assert len(event_ids) == len(set(event_ids))
    assert event_types.count("SUBMIT_REQUESTED") <= 1
    assert len(events) >= 1
