from __future__ import annotations

from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import Balance
from btcbot.domain.risk_budget import Mode
from btcbot.services.strategy_service import StrategyService


class _FakeStrategy:
    def generate_intents(self, context):
        return []


class _FakeMarketData:
    def __init__(self, prices: dict[str, tuple[Decimal, Decimal]]) -> None:
        self._prices = prices

    def get_best_bid_ask(self, symbol: str) -> tuple[Decimal, Decimal]:
        return self._prices[symbol]


class _FakeAccounting:
    def get_positions(self):
        return []



class _FakeStateStore:
    def __init__(self, mode: Mode) -> None:
        self._mode = mode

    def find_open_or_unknown_orders(self, *args, **kwargs):
        return []

    def get_latest_risk_mode(self) -> Mode:
        return self._mode


def _settings() -> Settings:
    return Settings(
        DRY_RUN=True,
        SYMBOLS="BTCTRY,ETHTRY,SOLTRY,AVAXTRY,ADATRY",
        PORTFOLIO_TARGETS="BTCTRY:0.4,ETHTRY:0.2,SOLTRY:0.2,AVAXTRY:0.1,ADATRY:0.1",
        TRY_CASH_TARGET="0",
        TRY_CASH_MAX="0",
        MAX_ORDERS_PER_CYCLE=10,
        NOTIONAL_CAP_TRY_PER_CYCLE="100000",
        MIN_ORDER_NOTIONAL_TRY=10,
        MAX_POSITION_NOTIONAL_TRY="100000",
        STAGE7_ORDER_OFFSET_BPS="5",
    )


def test_strategy_service_generates_rebalance_intents_from_plan_actions() -> None:
    settings = _settings()
    service = StrategyService(
        strategy=_FakeStrategy(),
        settings=settings,
        market_data_service=_FakeMarketData(
            {
                "BTCTRY": (Decimal("100"), Decimal("100")),
                "ETHTRY": (Decimal("100"), Decimal("100")),
                "SOLTRY": (Decimal("100"), Decimal("100")),
                "AVAXTRY": (Decimal("100"), Decimal("100")),
                "ADATRY": (Decimal("100"), Decimal("100")),
            }
        ),
        accounting_service=_FakeAccounting(),
        state_store=_FakeStateStore(Mode.NORMAL),
    )

    intents = service.generate(
        cycle_id="c1",
        symbols=["BTCTRY", "ETHTRY", "SOLTRY", "AVAXTRY", "ADATRY"],
        balances=[Balance(asset="TRY", free=1000)],
    )

    assert intents
    assert all(intent.qty > 0 for intent in intents)
    assert all(intent.limit_price is not None for intent in intents)
    assert all(intent.reason.startswith("portfolio_rebalance:") for intent in intents)


def test_strategy_service_observe_only_produces_no_rebalance_intents() -> None:
    settings = _settings()
    service = StrategyService(
        strategy=_FakeStrategy(),
        settings=settings,
        market_data_service=_FakeMarketData(
            {
                "BTCTRY": (Decimal("100"), Decimal("100")),
                "ETHTRY": (Decimal("100"), Decimal("100")),
                "SOLTRY": (Decimal("100"), Decimal("100")),
                "AVAXTRY": (Decimal("100"), Decimal("100")),
                "ADATRY": (Decimal("100"), Decimal("100")),
            }
        ),
        accounting_service=_FakeAccounting(),
        state_store=_FakeStateStore(Mode.OBSERVE_ONLY),
    )

    intents = service.generate(
        cycle_id="c2",
        symbols=["BTCTRY", "ETHTRY", "SOLTRY", "AVAXTRY", "ADATRY"],
        balances=[Balance(asset="TRY", free=1000)],
    )

    assert intents == []


def test_strategy_service_reduce_risk_only_produces_sell_intents() -> None:
    settings = _settings()
    service = StrategyService(
        strategy=_FakeStrategy(),
        settings=settings,
        market_data_service=_FakeMarketData(
            {
                "BTCTRY": (Decimal("100"), Decimal("100")),
                "ETHTRY": (Decimal("100"), Decimal("100")),
                "SOLTRY": (Decimal("100"), Decimal("100")),
                "AVAXTRY": (Decimal("100"), Decimal("100")),
                "ADATRY": (Decimal("100"), Decimal("100")),
            }
        ),
        accounting_service=_FakeAccounting(),
        state_store=_FakeStateStore(Mode.REDUCE_RISK_ONLY),
    )

    intents = service.generate(
        cycle_id="c3",
        symbols=["BTCTRY", "ETHTRY", "SOLTRY", "AVAXTRY", "ADATRY"],
        balances=[Balance(asset="BTC", free=5), Balance(asset="TRY", free=0)],
    )

    assert intents
    assert all(intent.side.value == "sell" for intent in intents)
