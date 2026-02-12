from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import Order
from btcbot.domain.strategy_core import PositionSummary
from btcbot.services.decision_pipeline_service import DecisionPipelineService


def test_orchestrator_builds_order_requests_and_report() -> None:
    fixed_now = datetime(2024, 1, 1, tzinfo=UTC)
    service = DecisionPipelineService(
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="BTC_TRY"),
        now_provider=lambda: fixed_now,
    )

    report = service.run_cycle(
        cycle_id="cycle-1",
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        open_orders=[],
        pair_info=[
            PairInfo(
                pairSymbol="BTCTRY",
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                status="TRADING",
            )
        ],
        bootstrap_enabled=True,
        live_mode=False,
    )

    assert report.selected_universe == ("BTCTRY",)
    assert len(report.intents) >= 1
    assert len(report.allocation_decisions) >= 1
    assert len(report.order_requests) >= 1
    order = report.order_requests[0]
    assert isinstance(order, Order)
    assert order.symbol == "BTCTRY"
    assert order.side in {"buy", "sell"}
    assert order.qty > Decimal("0")
    assert report.counters["accepted"] >= 1


def test_orchestrator_deterministic_for_same_input() -> None:
    fixed_now = datetime(2024, 1, 1, tzinfo=UTC)
    service = DecisionPipelineService(
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="BTC_TRY"),
        now_provider=lambda: fixed_now,
    )
    created_at = datetime(2024, 1, 1, tzinfo=UTC)
    open_orders = [
        Order(
            symbol="BTCTRY",
            side="sell",
            type="limit",
            price=Decimal("101"),
            qty=Decimal("0.01"),
            status="new",
            created_at=created_at,
            updated_at=created_at,
            client_order_id="x",
            mode="dry_run",
        )
    ]
    positions = {
        "BTCTRY": PositionSummary(symbol="BTCTRY", qty=Decimal("0"), avg_cost=Decimal("0"))
    }

    first = service.run_cycle(
        cycle_id="cycle-1",
        balances={"TRY": Decimal("600")},
        positions=positions,
        mark_prices={"BTCTRY": Decimal("100")},
        open_orders=open_orders,
        pair_info=None,
        bootstrap_enabled=True,
        live_mode=False,
    )
    second = service.run_cycle(
        cycle_id="cycle-1",
        balances={"TRY": Decimal("600")},
        positions=positions,
        mark_prices={"BTCTRY": Decimal("100")},
        open_orders=open_orders,
        pair_info=None,
        bootstrap_enabled=True,
        live_mode=False,
    )

    assert first.selected_universe == second.selected_universe
    assert first.intents == second.intents
    assert first.allocation_decisions == second.allocation_decisions
    assert first.order_requests == second.order_requests
