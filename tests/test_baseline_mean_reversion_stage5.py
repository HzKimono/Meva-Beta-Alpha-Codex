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


def _context(
    *,
    mark_price: str,
    anchor_price: str | None,
    has_position: bool,
    buy_count: int = 0,
    sell_count: int = 0,
    qty: str = "0.2",
    max_notional: str = "100",
    try_balance: str = "120",
) -> StrategyContext:
    return StrategyContext(
        timestamp=datetime.now(UTC),
        symbol="btc_try",
        mark_price=Decimal(mark_price),
        orderbook=OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("101")),
        balances={"TRY": Decimal(try_balance)},
        position=(
            PositionSummary(symbol="BTCTRY", qty=Decimal(qty), avg_cost=Decimal("100"))
            if has_position
            else None
        ),
        open_orders=OpenOrdersSummary(buy_count=buy_count, sell_count=sell_count),
        knobs=StrategyKnobs(
            anchor_price=Decimal(anchor_price) if anchor_price is not None else None,
            mean_reversion_bps=Decimal("50"),
            max_notional_try=Decimal(max_notional),
            bootstrap_notional_try=Decimal("25"),
        ),
    )


def test_bootstrap_intent_when_flat_has_expected_fields() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="100", anchor_price="100", has_position=False)
    )

    assert len(intents) == 1
    intent = intents[0]
    assert intent.intent_type == "place"
    assert intent.side == "buy"
    assert intent.symbol == "BTCTRY"
    assert intent.strategy_id == "baseline_mean_reversion_v1"
    assert intent.rationale == "bootstrap_position"
    assert intent.target_notional_try == Decimal("25")
    assert intent.confidence == Decimal("0.60")


def test_buy_guard_blocks_bootstrap_when_open_buy_exists() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="100", anchor_price="100", has_position=False, buy_count=1)
    )

    assert intents == []


def test_anchor_none_uses_mark_price_and_returns_no_intent_when_in_threshold() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="100", anchor_price=None, has_position=True)
    )

    assert intents == []


def test_emits_buy_on_negative_deviation() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="98", anchor_price="100", has_position=True)
    )

    assert len(intents) == 1
    intent = intents[0]
    assert intent.side == "buy"
    assert intent.intent_type == "place"
    assert intent.rationale == "mean_reversion_buy"
    assert intent.target_notional_try == Decimal("100")
    assert intent.confidence == Decimal("0.70")


def test_sell_guard_blocks_when_open_sell_exists() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="102", anchor_price="100", has_position=True, sell_count=1)
    )

    assert intents == []


def test_sell_intent_notional_is_capped_by_position_value() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(
            mark_price="102",
            anchor_price="100",
            has_position=True,
            qty="0.05",
            max_notional="100",
        )
    )

    assert len(intents) == 1
    assert intents[0].side == "sell"
    assert intents[0].target_notional_try == Decimal("5.10")


def test_no_intent_inside_threshold() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(mark_price="100.2", anchor_price="100", has_position=True)
    )

    assert intents == []


def test_sell_emits_when_try_balance_is_zero_and_position_exists() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(
            mark_price="102",
            anchor_price="100",
            has_position=True,
            qty="0.2",
            max_notional="100",
            try_balance="0",
        )
    )

    assert len(intents) == 1
    assert intents[0].side == "sell"
    assert intents[0].target_notional_try == Decimal("20.4")


def test_buy_does_not_emit_when_try_balance_is_zero() -> None:
    strategy = BaselineMeanReversionStrategy()

    intents = strategy.generate_intents(
        _context(
            mark_price="98",
            anchor_price="100",
            has_position=True,
            try_balance="0",
        )
    )

    assert intents == []
