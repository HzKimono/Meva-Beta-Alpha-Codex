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


def _context(*, timestamp: datetime | None = None) -> StrategyContext:
    return StrategyContext(
        timestamp=timestamp or datetime.now(UTC),
        symbol="btc_try",
        mark_price=Decimal("100"),
        orderbook=OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("101")),
        balances={"try": Decimal("1000")},
        position=PositionSummary(symbol="BTCTRY", qty=Decimal("0.1"), avg_cost=Decimal("95")),
        open_orders=OpenOrdersSummary(),
        knobs=StrategyKnobs(),
    )


def test_strategy_context_normalizes_symbol_and_freezes_balances() -> None:
    raw_balances = {"try": Decimal("1000")}
    context = StrategyContext(
        timestamp=datetime.now(UTC),
        symbol="btc_try",
        mark_price=Decimal("100"),
        orderbook=OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("101")),
        balances=raw_balances,
        position=PositionSummary(symbol="BTCTRY", qty=Decimal("0.1"), avg_cost=Decimal("95")),
        open_orders=OpenOrdersSummary(),
        knobs=StrategyKnobs(),
    )

    assert context.symbol == "BTCTRY"
    assert context.balances["TRY"] == Decimal("1000")

    raw_balances["try"] = Decimal("0")
    assert context.balances["TRY"] == Decimal("1000")

    with pytest.raises(TypeError):
        context.balances["TRY"] = Decimal("1")


def test_strategy_context_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _context(timestamp=datetime(2024, 1, 1, 0, 0, 0))


@pytest.mark.parametrize(
    ("bid", "ask", "message"),
    [
        ("0", "101", "best_bid"),
        ("99", "0", "best_ask"),
        ("102", "101", "<= best_ask"),
    ],
)
def test_orderbook_summary_validates_invariants(bid: str, ask: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        OrderBookSummary(best_bid=Decimal(bid), best_ask=Decimal(ask))


def test_intent_confidence_bounds() -> None:
    Intent(
        symbol="BTC_TRY",
        side="buy",
        intent_type="place",
        target_notional_try=Decimal("10"),
        rationale="x",
        strategy_id="s1",
        confidence=Decimal("0"),
    )
    Intent(
        symbol="BTC_TRY",
        side="sell",
        intent_type="place",
        target_notional_try=Decimal("10"),
        rationale="x",
        strategy_id="s1",
        confidence=Decimal("1"),
    )

    with pytest.raises(ValueError, match="confidence"):
        Intent(
            symbol="BTC_TRY",
            side="buy",
            intent_type="place",
            target_notional_try=Decimal("10"),
            rationale="x",
            strategy_id="s1",
            confidence=Decimal("-0.1"),
        )

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


@pytest.mark.parametrize("notional", [Decimal("0"), Decimal("-1")])
def test_intent_rejects_non_positive_notional_for_place(notional: Decimal) -> None:
    with pytest.raises(ValueError, match="target_notional_try"):
        Intent(
            symbol="BTC_TRY",
            side="buy",
            intent_type="place",
            target_notional_try=notional,
            rationale="x",
            strategy_id="s1",
        )


def test_intent_rejects_invalid_side_and_intent_type() -> None:
    with pytest.raises(ValueError, match="side"):
        Intent(
            symbol="BTC_TRY",
            side="hold",  # type: ignore[arg-type]
            intent_type="place",
            target_notional_try=Decimal("10"),
            rationale="x",
            strategy_id="s1",
        )

    with pytest.raises(ValueError, match="intent_type"):
        Intent(
            symbol="BTC_TRY",
            side="buy",
            intent_type="edit",  # type: ignore[arg-type]
            target_notional_try=Decimal("10"),
            rationale="x",
            strategy_id="s1",
        )


def test_intent_place_requires_notional_and_cancel_replace_forbid_it() -> None:
    with pytest.raises(ValueError, match="required"):
        Intent(
            symbol="BTC_TRY",
            side="buy",
            intent_type="place",
            target_notional_try=None,
            rationale="x",
            strategy_id="s1",
        )

    Intent(
        symbol="BTC_TRY",
        side="buy",
        intent_type="cancel",
        target_notional_try=None,
        rationale="x",
        strategy_id="s1",
    )
    Intent(
        symbol="BTC_TRY",
        side="sell",
        intent_type="replace",
        target_notional_try=None,
        rationale="x",
        strategy_id="s1",
    )

    with pytest.raises(ValueError, match="must be None"):
        Intent(
            symbol="BTC_TRY",
            side="sell",
            intent_type="cancel",
            target_notional_try=Decimal("1"),
            rationale="x",
            strategy_id="s1",
        )
