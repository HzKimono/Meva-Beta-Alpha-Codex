from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.accounting.models import PortfolioAccountingState
from btcbot.domain.risk_budget import Mode
from btcbot.risk.budget import RiskBudgetPolicy


def _state(equity: Decimal, treasury: Decimal = Decimal("100")) -> PortfolioAccountingState:
    return PortfolioAccountingState(
        as_of=datetime(2024, 1, 1, tzinfo=UTC),
        balances_try={"TRY": equity},
        locked_try={},
        treasury_try=treasury,
        trading_capital_try=equity,
        realized_pnl_try=Decimal("0"),
        unrealized_pnl_try=Decimal("0"),
        fees_try=Decimal("0"),
        funding_cost_try=Decimal("0"),
        slippage_try=Decimal("0"),
        symbols={},
    )


def test_profit_is_split_between_compounding_and_treasury() -> None:
    policy = RiskBudgetPolicy()
    trading, treasury = policy.apply_self_financing(
        trading_capital_try=Decimal("1000"),
        treasury_try=Decimal("200"),
        realized_pnl_delta_try=Decimal("100"),
    )
    assert trading == Decimal("1060.00000000")
    assert treasury == Decimal("240.00000000")


def test_loss_reduces_trading_capital_without_touching_treasury() -> None:
    policy = RiskBudgetPolicy()
    trading, treasury = policy.apply_self_financing(
        trading_capital_try=Decimal("1000"),
        treasury_try=Decimal("200"),
        realized_pnl_delta_try=Decimal("-80"),
    )
    assert trading == Decimal("920.00000000")
    assert treasury == Decimal("200.00000000")


def test_budget_halts_when_daily_loss_exceeds_limit() -> None:
    policy = RiskBudgetPolicy()
    decision = policy.evaluate(
        accounting=_state(Decimal("1000")),
        peak_equity_try=Decimal("1100"),
        realized_pnl_today_try=Decimal("-70"),
        consecutive_loss_streak=0,
        volatility_regime="normal",
    )
    assert decision.mode == Mode.OBSERVE_ONLY
    assert decision.position_sizing_multiplier == Decimal("0")
