from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.planning_kernel import Intent, PlanningContext, PlanningKernel
from btcbot.services.planning_kernel_adapters import (
    InMemoryExecutionPort,
    Stage4PlanConsumer,
    Stage7PlanConsumer,
)


@dataclass(frozen=True)
class _MarketData:
    mark_prices_try: dict[str, Decimal]
    symbol_rules: dict[str, dict[str, Decimal | str | int]]


@dataclass(frozen=True)
class _Portfolio:
    cash_try: Decimal
    positions_qty: dict[str, Decimal]
    open_orders: tuple[object, ...] = ()


class _UniverseSelector:
    def select(self, context: PlanningContext) -> list[str]:
        del context
        return ["BTCTRY", "ETHTRY"]


class _StrategyEngine:
    def generate_intents(self, context: PlanningContext, universe: list[str]) -> list[Intent]:
        del context
        return [
            Intent(
                symbol=symbol,
                side="BUY",
                target_notional_try=Decimal("100"),
                rationale="test",
                strategy_id="test_strategy",
            )
            for symbol in universe
        ]


class _Allocator:
    def allocate(self, context: PlanningContext, intents: list[Intent]) -> list[Intent]:
        del context
        return intents


class _OrderIntentBuilder:
    def build(self, context: PlanningContext, intents: list[Intent]) -> list[object]:
        from btcbot.domain.order_intent import OrderIntent

        out: list[OrderIntent] = []
        for idx, intent in enumerate(intents):
            price = context.market_data.mark_prices_try[intent.symbol]
            qty = (intent.target_notional_try / price).quantize(Decimal("0.0001"))
            out.append(
                OrderIntent(
                    cycle_id=context.cycle_id,
                    symbol=intent.symbol,
                    side="BUY",
                    order_type="LIMIT",
                    price_try=price,
                    qty=qty,
                    notional_try=price * qty,
                    client_order_id=f"cid-{idx}",
                    reason=intent.rationale,
                    constraints_applied={},
                )
            )
        return out


def test_stage4_and_stage7_consumers_submit_identical_order_intents() -> None:
    kernel = PlanningKernel(
        universe_selector=_UniverseSelector(),
        strategy_engine=_StrategyEngine(),
        allocator=_Allocator(),
        order_intent_builder=_OrderIntentBuilder(),
    )
    context = PlanningContext(
        cycle_id="cycle-1",
        now_utc=datetime.now(UTC),
        market_data=_MarketData(
            mark_prices_try={"BTCTRY": Decimal("1000000"), "ETHTRY": Decimal("50000")},
            symbol_rules={},
        ),
        portfolio=_Portfolio(cash_try=Decimal("1000"), positions_qty={}),
    )
    plan = kernel.plan(context)

    stage4_exec = InMemoryExecutionPort()
    stage7_exec = InMemoryExecutionPort()
    stage4_ids = Stage4PlanConsumer(stage4_exec).consume(plan)
    stage7_ids = Stage7PlanConsumer(stage7_exec).consume(plan)

    assert stage4_ids == stage7_ids
    assert stage4_exec.reconcile() == stage7_exec.reconcile()
