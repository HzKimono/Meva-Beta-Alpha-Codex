from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.order_intent import OrderIntent
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.rate_limiter import TokenBucketRateLimiter
from btcbot.services.state_store import StateStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


def _intent(cid: str) -> OrderIntent:
    return OrderIntent(
        cycle_id="cycle-1",
        symbol="BTCTRY",
        side="BUY",
        order_type="LIMIT",
        price_try=Decimal("100"),
        qty=Decimal("1"),
        notional_try=Decimal("100"),
        client_order_id=cid,
        reason="test",
        constraints_applied={},
    )


def test_burst_then_throttle_then_allowed(tmp_path) -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(rate_per_sec=1.0, burst=1, time_source=clock.time)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    settings = Settings(
        DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_RATE_LIMIT_RPS=1, STAGE7_RATE_LIMIT_BURST=1
    )
    oms = OMSService(rate_limiter=limiter)
    now = datetime(2024, 1, 1, tzinfo=UTC)

    _, events_1 = oms.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[_intent("s7:c1:BTCTRY:BUY:feed00000001")],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )
    _, events_2 = oms.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[_intent("s7:c1:BTCTRY:BUY:feed00000002")],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    clock.now += 1.2
    _, events_3 = oms.process_intents(
        cycle_id="cycle-1",
        now_utc=now,
        intents=[_intent("s7:c1:BTCTRY:BUY:feed00000003")],
        market_sim=Stage7MarketSimulator({"BTCTRY": Decimal("100")}),
        state_store=store,
        settings=settings,
    )

    assert any(e.event_type == "SUBMIT_REQUESTED" for e in events_1)
    assert any(e.event_type == "THROTTLED" for e in events_2)
    assert any(e.event_type == "SUBMIT_REQUESTED" for e in events_3)
