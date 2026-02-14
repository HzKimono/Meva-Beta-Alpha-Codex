from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.order_intent import OrderIntent
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.state_store import StateStore


def _intent(*, cid: str, price: str = "100") -> OrderIntent:
    return OrderIntent(
        cycle_id="cycle-1",
        symbol="BTCTRY",
        side="BUY",
        order_type="LIMIT",
        price_try=Decimal(price),
        qty=Decimal("1"),
        notional_try=Decimal(price),
        client_order_id=cid,
        reason="test",
        constraints_applied={},
    )


def test_same_intent_twice_only_one_submitted(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    oms = OMSService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    intent = _intent(cid="s7:c1:BTCTRY:BUY:deadbeef0001")

    _, events_first = oms.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[intent],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )
    _, events_second = oms.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[intent],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    submitted = [e for e in events_first + events_second if e.event_type == "SUBMIT_REQUESTED"]
    duplicates = [e for e in events_second if e.event_type == "DUPLICATE_IGNORED"]
    assert len(submitted) == 1
    assert len(duplicates) == 1


def test_same_key_different_payload_conflict_is_isolated(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    oms = OMSService()
    now = datetime(2024, 1, 1, tzinfo=UTC)

    oms.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[_intent(cid="s7:c1:BTCTRY:BUY:deadbeef0002", price="100")],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    _, events = oms.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[
            _intent(cid="s7:c1:BTCTRY:BUY:deadbeef0002", price="101"),
            _intent(cid="s7:c1:BTCTRY:BUY:deadbeef0003", price="100"),
        ],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    assert any(e.event_type == "IDEMPOTENCY_CONFLICT" for e in events)
    assert any(
        e.event_type == "SUBMIT_REQUESTED" and e.client_order_id.endswith("deadbeef0003")
        for e in events
    )
