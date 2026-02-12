from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.domain.strategy_core import (
    Intent,
    OpenOrdersSummary,
    OrderBookSummary,
    StrategyContext,
    StrategyKnobs,
)
from btcbot.strategies.stage5_core import StrategyRegistry


class _StubStrategy:
    def __init__(self, strategy_id: str, side: str) -> None:
        self.id = strategy_id
        self._side = side

    def generate_intents(self, context: StrategyContext) -> list[Intent]:
        return [
            Intent(
                symbol=context.symbol,
                side=self._side,
                intent_type="place",
                target_notional_try=Decimal("10"),
                rationale=f"from_{self.id}",
                strategy_id=self.id,
            )
        ]


def _context() -> StrategyContext:
    return StrategyContext(
        timestamp=datetime.now(UTC),
        symbol="BTC_TRY",
        mark_price=Decimal("100"),
        orderbook=OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("101")),
        balances={"TRY": Decimal("100")},
        position=None,
        open_orders=OpenOrdersSummary(),
        knobs=StrategyKnobs(),
    )


def test_registry_enable_disable() -> None:
    registry = StrategyRegistry()
    alpha = _StubStrategy("alpha", "buy")
    beta = _StubStrategy("beta", "sell")

    registry.register(alpha)
    registry.register(beta, enabled=False)

    intents = registry.generate_intents(_context())
    assert [item.strategy_id for item in intents] == ["alpha"]

    registry.set_enabled("beta", enabled=True)
    intents = registry.generate_intents(_context())
    assert [item.strategy_id for item in intents] == ["alpha", "beta"]


def test_registry_is_deterministic_for_same_input() -> None:
    registry = StrategyRegistry()
    registry.register(_StubStrategy("zeta", "sell"), weight=1)
    registry.register(_StubStrategy("alpha", "buy"), weight=2)

    first = registry.generate_intents(_context())
    second = registry.generate_intents(_context())

    assert first == second
    assert [item.strategy_id for item in first] == ["alpha", "zeta"]


def test_registry_equal_weight_tiebreak_is_by_strategy_id() -> None:
    registry = StrategyRegistry()
    registry.register(_StubStrategy("zeta", "sell"), weight=5)
    registry.register(_StubStrategy("alpha", "buy"), weight=5)

    intents = registry.generate_intents(_context())

    assert [item.strategy_id for item in intents] == ["alpha", "zeta"]


def test_registry_rejects_negative_weight() -> None:
    registry = StrategyRegistry()

    with pytest.raises(ValueError, match=">= 0"):
        registry.register(_StubStrategy("alpha", "buy"), weight=-1)


def test_registry_rejects_duplicate_register_and_supports_replace() -> None:
    registry = StrategyRegistry()
    registry.register(_StubStrategy("alpha", "buy"), weight=1)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_StubStrategy("alpha", "sell"), weight=2)

    registry.replace(_StubStrategy("alpha", "sell"), weight=2)
    intents = registry.generate_intents(_context())
    assert intents[0].side == "sell"


def test_set_enabled_missing_id_has_clear_keyerror() -> None:
    registry = StrategyRegistry()

    with pytest.raises(KeyError, match="missing"):
        registry.set_enabled("missing", enabled=True)
