from __future__ import annotations

from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.accounting import Position
from btcbot.domain.models import Balance, OrderSide
from btcbot.strategies.context import StrategyContext
from btcbot.strategies.profit_v1 import ProfitAwareStrategyV1


def test_strategy_generates_profitable_sell() -> None:
    strategy = ProfitAwareStrategyV1()
    settings = Settings(MIN_PROFIT_BPS=30)
    context = StrategyContext(
        cycle_id="c1",
        symbols=["BTC_TRY"],
        orderbooks={"BTCTRY": (Decimal("103"), Decimal("104"))},
        positions={
            "BTCTRY": Position(
                symbol="BTCTRY",
                qty=Decimal("4"),
                avg_cost=Decimal("100"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                fees_paid=Decimal("0"),
            )
        },
        balances=[Balance(asset="TRY", free=1000.0)],
        open_orders={},
        settings=settings,
    )

    intents = strategy.generate_intents(context)
    assert len(intents) == 1
    assert intents[0].side == OrderSide.SELL
    assert intents[0].qty == Decimal("1.00")


def test_strategy_conservative_buy_when_flat() -> None:
    strategy = ProfitAwareStrategyV1()
    settings = Settings()
    context = StrategyContext(
        cycle_id="c1",
        symbols=["ETH_TRY"],
        orderbooks={"ETHTRY": (Decimal("100"), Decimal("100.5"))},
        positions={},
        balances=[Balance(asset="TRY", free=200.0)],
        open_orders={},
        settings=settings,
    )
    intents = strategy.generate_intents(context)
    assert len(intents) == 1
    assert intents[0].side == OrderSide.BUY
    assert intents[0].limit_price == Decimal("100.5")
    assert intents[0].qty == Decimal("100") / Decimal("100.5")


def test_strategy_does_not_buy_when_spread_too_large() -> None:
    strategy = ProfitAwareStrategyV1()
    settings = Settings()
    context = StrategyContext(
        cycle_id="c1",
        symbols=["ETH_TRY"],
        orderbooks={"ETHTRY": (Decimal("100"), Decimal("102"))},
        positions={},
        balances=[Balance(asset="TRY", free=200.0)],
        open_orders={},
        settings=settings,
    )

    assert strategy.generate_intents(context) == []


def test_strategy_does_not_buy_when_try_balance_zero() -> None:
    strategy = ProfitAwareStrategyV1()
    settings = Settings()
    context = StrategyContext(
        cycle_id="c1",
        symbols=["ETH_TRY"],
        orderbooks={"ETHTRY": (Decimal("100"), Decimal("100.5"))},
        positions={},
        balances=[Balance(asset="TRY", free=0.0)],
        open_orders={},
        settings=settings,
    )

    assert strategy.generate_intents(context) == []
