from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.strategy_core import (
    OpenOrdersSummary,
    OrderBookSummary,
    PositionSummary,
    StrategyContext,
    StrategyKnobs,
)
from btcbot.strategies.baseline_mean_reversion import BaselineMeanReversionStrategy


def _context(*, mark_price: str, anchor_price: str | None, has_position: bool) -> StrategyContext:
    return StrategyContext(
        timestamp=datetime.now(UTC),
        symbol="BTC_TRY",
        mark_price=Decimal(mark_price),
        orderbook=OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("101")),
        balances={"TRY": Decimal("120")},
        position=(
            PositionSummary(symbol="BTCTRY", qty=Decimal("0.2"), avg_cost=Decimal("100"))
            if has_position
            else None
        ),
        open_orders=OpenOrdersSummary(),
        knobs=StrategyKnobs(
            anchor_price=Decimal(anchor_price) if anchor_price is not None else None,
            mean_reversion_bps=Decimal("50"),
            max_notional_try=Decimal("100"),
            bootstrap_notional_try=Decimal("25"),
        ),
    )


def test_bootstrap_intent_when_flat() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="100", anchor_price="100", has_position=False)
    )

    assert len(intents) == 1
    assert intents[0].side == "buy"
    assert intents[0].rationale == "bootstrap_position"
    assert intents[0].target_notional_try == Decimal("25")


def test_emits_buy_on_negative_deviation() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="98", anchor_price="100", has_position=True)
    )

    assert len(intents) == 1
    assert intents[0].side == "buy"
    assert intents[0].rationale == "mean_reversion_buy"
    assert intents[0].target_notional_try == Decimal("100")


def test_emits_sell_on_positive_deviation() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="102", anchor_price="100", has_position=True)
    )

    assert len(intents) == 1
    assert intents[0].side == "sell"
    assert intents[0].rationale == "mean_reversion_sell"


def test_no_intent_inside_threshold() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="100.2", anchor_price="100", has_position=True)
    )

    assert intents == []
