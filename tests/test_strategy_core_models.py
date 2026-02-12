from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.domain.strategy_core import (
    Intent,
    OpenOrdersSummary,
    OrderBookSummary,
    PositionSummary,
    StrategyContext,
    StrategyKnobs,
)


def test_strategy_context_normalizes_symbol() -> None:
    context = StrategyContext(
        timestamp=datetime.now(UTC),
        symbol="btc_try",
        mark_price=Decimal("100"),
        orderbook=OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("101")),
        balances={"TRY": Decimal("1000")},
        position=PositionSummary(symbol="BTCTRY", qty=Decimal("0.1"), avg_cost=Decimal("95")),
        open_orders=OpenOrdersSummary(),
        knobs=StrategyKnobs(),
    )

    assert context.symbol == "BTCTRY"


def test_intent_validates_confidence_bounds() -> None:
    with pytest.raises(ValueError, match="confidence"):
        Intent(
            symbol="BTC_TRY",
            side="buy",
            intent_type="place",
            target_notional_try=Decimal("10"),
            rationale="x",
            strategy_id="s1",
            confidence=Decimal("1.1"),
        )


def test_intent_rejects_non_positive_notional() -> None:
    with pytest.raises(ValueError, match="target_notional_try"):
        Intent(
            symbol="BTC_TRY",
            side="buy",
            intent_type="place",
            target_notional_try=Decimal("0"),
            rationale="x",
            strategy_id="s1",
        )
