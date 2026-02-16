from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.accounting.models import PortfolioAccountingState, quantize_money
from btcbot.domain.risk_budget import Mode


@dataclass(frozen=True)
class SelfFinancingPolicy:
    profit_compound_ratio: Decimal = Decimal("0.60")
    profit_treasury_ratio: Decimal = Decimal("0.40")
    loss_streak_reduce_multiplier: Decimal = Decimal("0.50")
    drawdown_reduce_multiplier: Decimal = Decimal("0.25")
    halt_drawdown_ratio: Decimal = Decimal("0.15")
    halt_daily_loss_ratio: Decimal = Decimal("0.05")
    max_exposure_ratio: Decimal = Decimal("1.20")
    max_order_ratio: Decimal = Decimal("0.20")


@dataclass(frozen=True)
class RiskBudgetView:
    trading_capital_try: Decimal
    treasury_try: Decimal
    available_risk_capital_try: Decimal
    daily_loss_limit_try: Decimal
    drawdown_halt_limit_try: Decimal
    max_gross_exposure_try: Decimal
    max_order_notional_try: Decimal
    position_sizing_multiplier: Decimal
    mode: Mode


class RiskBudgetPolicy:
    def __init__(self, policy: SelfFinancingPolicy | None = None) -> None:
        self.policy = policy or SelfFinancingPolicy()

    def evaluate(
        self,
        *,
        accounting: PortfolioAccountingState,
        peak_equity_try: Decimal,
        realized_pnl_today_try: Decimal,
        consecutive_loss_streak: int,
        volatility_regime: str,
    ) -> RiskBudgetView:
        p = self.policy
        capital = quantize_money(max(accounting.trading_capital_try, Decimal("0")))
        treasury = quantize_money(max(accounting.treasury_try, Decimal("0")))
        available = quantize_money(max(capital - treasury, Decimal("0")))

        daily_loss_limit = quantize_money(capital * p.halt_daily_loss_ratio)
        drawdown_limit = quantize_money(max(peak_equity_try, Decimal("0")) * p.halt_drawdown_ratio)
        max_exposure = quantize_money(available * p.max_exposure_ratio)
        max_order = quantize_money(available * p.max_order_ratio)

        multiplier = Decimal("1")
        if consecutive_loss_streak >= 3:
            multiplier = min(multiplier, p.loss_streak_reduce_multiplier)
        if volatility_regime.lower() in {"high", "stressed"}:
            multiplier = min(multiplier, p.drawdown_reduce_multiplier)

        mode = Mode.NORMAL
        if realized_pnl_today_try <= -daily_loss_limit:
            mode = Mode.OBSERVE_ONLY
            multiplier = Decimal("0")

        drawdown_try = max(Decimal("0"), peak_equity_try - accounting.equity_try)
        if drawdown_try >= drawdown_limit:
            mode = Mode.OBSERVE_ONLY
            multiplier = Decimal("0")
        elif multiplier < Decimal("1") and mode == Mode.NORMAL:
            mode = Mode.REDUCE_RISK_ONLY

        return RiskBudgetView(
            trading_capital_try=capital,
            treasury_try=treasury,
            available_risk_capital_try=available,
            daily_loss_limit_try=daily_loss_limit,
            drawdown_halt_limit_try=drawdown_limit,
            max_gross_exposure_try=quantize_money(max_exposure * multiplier),
            max_order_notional_try=quantize_money(max_order * multiplier),
            position_sizing_multiplier=multiplier,
            mode=mode,
        )

    def apply_self_financing(
        self,
        *,
        trading_capital_try: Decimal,
        treasury_try: Decimal,
        realized_pnl_delta_try: Decimal,
    ) -> tuple[Decimal, Decimal]:
        p = self.policy
        trading_capital = quantize_money(trading_capital_try)
        treasury = quantize_money(treasury_try)
        delta = quantize_money(realized_pnl_delta_try)

        if delta > 0:
            compound = quantize_money(delta * p.profit_compound_ratio)
            reserve = quantize_money(delta * p.profit_treasury_ratio)
            return quantize_money(trading_capital + compound), quantize_money(treasury + reserve)

        return quantize_money(trading_capital + delta), treasury
