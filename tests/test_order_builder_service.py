from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.portfolio_policy_models import PortfolioPlan, RebalanceAction, TargetAllocation
from btcbot.domain.risk_budget import Mode
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.order_builder_service import OrderBuilderService


class _Pair:
    def __init__(self, symbol: str) -> None:
        self.pair_symbol = symbol
        self.name = symbol.replace("_", "")
        self.nameNormalized = symbol
        self.numeratorScale = 6
        self.denominatorScale = 2
        self.minTotalAmount = Decimal("100")
        self.tickSize = Decimal("0.1")
        self.stepSize = Decimal("0.01")
        self.minQuantity = Decimal("0.01")
        self.maxQuantity = None


class _Exchange:
    def get_exchange_info(self):
        return [_Pair("BTC_TRY")]


def _plan(actions: list[RebalanceAction]) -> PortfolioPlan:
    return PortfolioPlan(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        universe=["BTCTRY"],
        cash_target_try=Decimal("0"),
        allocations=[
            TargetAllocation(
                symbol="BTCTRY", weight=Decimal("1"), target_notional_try=Decimal("1000")
            )
        ],
        actions=actions,
        constraints_summary={},
        notes=[],
    )


def test_tick_lot_quantization_correctness() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTC_TRY",
                    side="BUY",
                    target_notional_try=Decimal("1000"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("101.23")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert len(intents) == 1
    assert intents[0].price_try == Decimal("101.1")
    assert intents[0].qty == Decimal("9.89")


def test_min_notional_skip() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTC_TRY",
                    side="BUY",
                    target_notional_try=Decimal("10"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("101")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert intents[0].skipped is True
    assert intents[0].skip_reason == "min_notional"


def test_deterministic_client_order_id() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    kwargs = dict(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTC_TRY",
                    side="SELL",
                    target_notional_try=Decimal("500"),
                    est_qty=Decimal("1"),
                    reason="trim",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    a = builder.build_intents(**kwargs)[0]
    b = builder.build_intents(**kwargs)[0]
    assert a.client_order_id == b.client_order_id


def test_sell_first_preserved() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTC_TRY",
                    side="BUY",
                    target_notional_try=Decimal("1000"),
                    est_qty=Decimal("1"),
                    reason="buy",
                ),
                RebalanceAction(
                    symbol="BTC_TRY",
                    side="SELL",
                    target_notional_try=Decimal("100"),
                    est_qty=Decimal("1"),
                    reason="sell",
                ),
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert [intent.side for intent in intents][:2] == ["SELL", "BUY"]


def test_mode_gating_observe_and_reduce() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    plan = _plan(
        [
            RebalanceAction(
                symbol="BTC_TRY",
                side="BUY",
                target_notional_try=Decimal("500"),
                est_qty=Decimal("1"),
                reason="buy",
            ),
            RebalanceAction(
                symbol="BTC_TRY",
                side="SELL",
                target_notional_try=Decimal("500"),
                est_qty=Decimal("1"),
                reason="sell",
            ),
        ]
    )
    observe = builder.build_intents(
        cycle_id="c1",
        plan=plan,
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.OBSERVE_ONLY,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    reduce_only = builder.build_intents(
        cycle_id="c1",
        plan=plan,
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.REDUCE_RISK_ONLY,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert observe == []
    assert all(
        intent.side == "SELL" or intent.skip_reason == "mode_reduce_risk_only"
        for intent in reduce_only
    )
