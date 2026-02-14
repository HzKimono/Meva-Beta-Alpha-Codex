from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.order_state import make_event_id
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.state_store import StateStore


def _intent(*, cid: str, symbol: str = "BTCTRY", price: str = "100", qty: str = "1") -> OrderIntent:
    return OrderIntent(
        cycle_id="cycle-1",
        symbol=symbol,
        side="BUY",
        order_type="LIMIT",
        price_try=Decimal(price),
        qty=Decimal(qty),
        notional_try=Decimal(price) * Decimal(qty),
        client_order_id=cid,
        reason="test",
        constraints_applied={},
    )


def test_process_intents_deterministic_and_idempotent(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "oms.db"))
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, SYMBOLS="BTC_TRY")
    svc = OMSService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    intents = [
        _intent(cid="s7:c1:BTCTRY:BUY:aaa000000000"),
        _intent(cid="s7:c1:BTCTRY:BUY:bbb000000000"),
    ]

    orders_1, events_1 = svc.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=intents,
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )
    with store._connect() as conn:
        rows_before = conn.execute(
            """
            SELECT client_order_id, event_id
            FROM stage7_order_events
            ORDER BY client_order_id, ts, event_id
            """
        ).fetchall()

    orders_2, events_2 = svc.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=intents,
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    assert len(orders_1) == 2
    assert len(events_1) >= 6
    assert all(e.event_type == "DUPLICATE_IGNORED" for e in events_2)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stage7_orders").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM stage7_order_events").fetchone()[0] == len(
            events_1
        ) + len(events_2)
        rows_after = conn.execute(
            """
            SELECT client_order_id, event_id
            FROM stage7_order_events
            ORDER BY client_order_id, ts, event_id
            """
        ).fetchall()
    assert len(rows_after) == len(rows_before) + len(events_2)
    for client_order_id in {str(row["client_order_id"]) for row in rows_before}:
        client_event_ids = {
            str(row["event_id"])
            for row in rows_before
            if str(row["client_order_id"]) == client_order_id
        }
        assert make_event_id(client_order_id, 1, "SUBMIT_REQUESTED") in client_event_ids
        assert make_event_id(client_order_id, 2, "ACKED") in client_event_ids
    assert [o.order_id for o in orders_1] == [o.order_id for o in orders_2]


def test_partial_fill_then_filled(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "oms_partial.db"))
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, SYMBOLS="BTC_TRY")
    svc = OMSService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    intent = _intent(cid="s7:c1:BTCTRY:BUY:aaa00000000e", qty="2")

    orders, events = svc.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[intent],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    assert orders[0].status.value == "FILLED"
    assert any(e.event_type == "PARTIAL_FILL" for e in events)
    assert any(e.event_type == "FILLED" for e in events)


def test_invalid_intent_rejected(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "oms_reject.db"))
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, SYMBOLS="BTC_TRY")
    svc = OMSService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    intent = _intent(cid="s7:c1:BTCTRY:BUY:ccc000000001", price="0", qty="1")

    orders, events = svc.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[intent],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    assert orders[0].status.value == "REJECTED"
    assert events[-1].event_type == "REJECTED"
