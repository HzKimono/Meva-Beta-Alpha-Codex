from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.allocation import AllocationResult
from btcbot.domain.models import PairInfo
from btcbot.services.decision_pipeline_service import DecisionPipelineService


@dataclass
class _Capture:
    max_total: Decimal = Decimal("0")
    max_cycle: Decimal = Decimal("0")


class _SpyAllocationService:
    capture = _Capture()

    @staticmethod
    def allocate(**kwargs) -> AllocationResult:
        knobs = kwargs["knobs"]
        _SpyAllocationService.capture.max_total = knobs.max_total_notional_try_per_cycle
        _SpyAllocationService.capture.max_cycle = knobs.max_try_per_cycle
        return AllocationResult(
            actions=tuple(),
            decisions=tuple(),
            counters={},
            cash_try=Decimal("0"),
            try_cash_target=Decimal("0"),
            investable_total_try=Decimal("0"),
            investable_this_cycle_try=Decimal("0"),
            deploy_budget_try=Decimal("0"),
            planned_total_try=Decimal("0"),
            unused_budget_try=Decimal("0"),
            investable_usage_reason="use_all",
        )


def test_budget_multiplier_scales_allocator_caps() -> None:
    service = DecisionPipelineService(
        settings=Settings(
            notional_cap_try_per_cycle=Decimal("1000"),
            MAX_TRY_PER_CYCLE=Decimal("400"),
        ),
        allocation_service=_SpyAllocationService,
    )
    service._generate_intents = lambda **_: []
    service._select_universe = lambda **_: []

    service.run_cycle(
        cycle_id="c1",
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={},
        open_orders=[],
        pair_info=[],
        bootstrap_enabled=False,
        live_mode=False,
        budget_notional_multiplier=Decimal("0.25"),
    )

    assert _SpyAllocationService.capture.max_total == Decimal("250.00")
    assert _SpyAllocationService.capture.max_cycle == Decimal("100.00")


def test_aggressive_path_scales_deploy_budget_with_multiplier() -> None:
    service = DecisionPipelineService(
        settings=Settings(
            symbols=["BTCTRY", "ETHTRY"],
            try_cash_target=Decimal("100"),
            max_orders_per_cycle=10,
            min_order_notional_try=Decimal("10"),
        )
    )

    pair_info = [
        PairInfo.model_validate(
            {
                "pairSymbol": "BTCTRY",
                "numeratorScale": 8,
                "denominatorScale": 2,
                "minTotalAmount": "10",
                "minQuantity": "0.0001",
                "tickSize": "0.01",
                "stepSize": "0.0001",
            }
        ),
        PairInfo.model_validate(
            {
                "pairSymbol": "ETHTRY",
                "numeratorScale": 8,
                "denominatorScale": 2,
                "minTotalAmount": "10",
                "minQuantity": "0.0001",
                "tickSize": "0.01",
                "stepSize": "0.0001",
            }
        ),
    ]

    baseline = service.run_cycle(
        cycle_id="c1",
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100")},
        open_orders=[],
        pair_info=pair_info,
        bootstrap_enabled=False,
        live_mode=False,
        aggressive_scores={"BTCTRY": Decimal("1"), "ETHTRY": Decimal("1")},
        budget_notional_multiplier=Decimal("1"),
    )
    scaled = service.run_cycle(
        cycle_id="c2",
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100")},
        open_orders=[],
        pair_info=pair_info,
        bootstrap_enabled=False,
        live_mode=False,
        aggressive_scores={"BTCTRY": Decimal("1"), "ETHTRY": Decimal("1")},
        budget_notional_multiplier=Decimal("0.25"),
    )

    assert scaled.deploy_budget_try < baseline.deploy_budget_try
    assert scaled.deploy_budget_try == baseline.deploy_budget_try * Decimal("0.25")
