from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.action_to_order import build_exchange_rules
from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import Order, Quantizer
from btcbot.domain.strategy_core import PositionSummary
from btcbot.services.decision_pipeline_service import DecisionPipelineService


def _btc_pair_info() -> PairInfo:
    return PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=4,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        status="TRADING",
    )


def test_orchestrator_builds_quantized_order_requests_and_report() -> None:
    fixed_now = datetime(2024, 1, 1, tzinfo=UTC)
    service = DecisionPipelineService(
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="BTC_TRY"),
        now_provider=lambda: fixed_now,
    )
    pair = PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=4,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        status="TRADING",
    )

    report = service.run_cycle(
        cycle_id="cycle-1",
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100.129")},
        open_orders=[],
        pair_info=[pair],
        bootstrap_enabled=True,
        live_mode=False,
    )

    assert report.selected_universe == ("BTCTRY",)
    assert len(report.intents) >= 1
    assert len(report.allocation_decisions) >= 1
    assert len(report.order_requests) >= 1
    assert report.mapped_orders_count == len(report.order_requests)
    assert report.dropped_actions_count == 0

    order = report.order_requests[0]
    rules = build_exchange_rules(pair)
    assert isinstance(order, Order)
    assert order.symbol == "BTCTRY"
    assert order.price == Quantizer.quantize_price(Decimal("100.129"), rules)
    assert Quantizer.validate_min_notional(order.price, order.qty, rules) is True
    assert len(order.client_order_id or "") <= 64
    assert all(ch.isalnum() or ch in {":", "|", "-", "_"} for ch in (order.client_order_id or ""))


def test_orchestrator_reports_dropped_min_notional_mapping() -> None:
    fixed_now = datetime(2024, 1, 1, tzinfo=UTC)
    service = DecisionPipelineService(
        settings=Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="BTC_TRY"),
        now_provider=lambda: fixed_now,
    )
    pair = PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=4,
        denominatorScale=2,
        minTotalAmount=Decimal("50"),
        status="TRADING",
    )

    report = service.run_cycle(
        cycle_id="cycle-1",
        balances={"TRY": Decimal("320")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        open_orders=[],
        pair_info=[pair],
        bootstrap_enabled=True,
        live_mode=False,
    )

    assert len(report.allocation_actions) == 1
    assert len(report.order_requests) == 0
    assert report.dropped_actions_count == 1
    assert report.dropped_reasons["dropped_min_notional_after_quantize"] == 1


def test_orchestrator_skips_missing_pair_info() -> None:
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
        pair_info=[],
        bootstrap_enabled=True,
        live_mode=False,
    )

    assert len(report.allocation_actions) == 1
    assert len(report.order_requests) == 0
    assert report.dropped_actions_count == 1
    assert report.dropped_reasons["dropped_missing_pair_info"] == 1


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
        pair_info=[_btc_pair_info()],
        bootstrap_enabled=True,
        live_mode=False,
    )
    second = service.run_cycle(
        cycle_id="cycle-1",
        balances={"TRY": Decimal("600")},
        positions=positions,
        mark_prices={"BTCTRY": Decimal("100")},
        open_orders=open_orders,
        pair_info=[_btc_pair_info()],
        bootstrap_enabled=True,
        live_mode=False,
    )

    assert first.selected_universe == second.selected_universe
    assert first.intents == second.intents
    assert first.allocation_decisions == second.allocation_decisions
    assert first.order_requests == second.order_requests
    assert first.dropped_reasons == second.dropped_reasons
