from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from btcbot.accounting.models import PortfolioAccountingState
from btcbot.domain.risk_budget import Mode, RiskDecision, RiskLimits, RiskSignals, decide_mode
from btcbot.domain.stage4 import Position
from btcbot.risk.budget import RiskBudgetPolicy, RiskBudgetView
from btcbot.services.ledger_service import PnlReport
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetDecision:
    risk_decision: RiskDecision
    budget_view: RiskBudgetView

    @property
    def mode(self) -> Mode:
        return self.risk_decision.mode

    @property
    def position_sizing_multiplier(self) -> Decimal:
        return self.budget_view.position_sizing_multiplier

    @property
    def max_order_notional_try(self) -> Decimal:
        return self.budget_view.max_order_notional_try

    @property
    def available_risk_capital_try(self) -> Decimal:
        return self.budget_view.available_risk_capital_try


class RiskBudgetService:
    def __init__(
        self,
        state_store: StateStore,
        *,
        now_provider: Callable[[], datetime] | None = None,
        budget_policy: RiskBudgetPolicy | None = None,
    ) -> None:
        self.state_store = state_store
        self.now_provider = now_provider or (lambda: datetime.now(UTC))
        self.budget_policy = budget_policy or RiskBudgetPolicy()

    def compute_decision(
        self,
        *,
        limits: RiskLimits,
        pnl_report: PnlReport,
        positions: list[Position],
        mark_prices: dict[str, Decimal],
        realized_today_try: Decimal,
        kill_switch_active: bool,
    ) -> tuple[BudgetDecision, Mode | None, Decimal, Decimal, date]:
        current = self.state_store.get_risk_state_current()
        prev_mode_raw = current.get("current_mode")
        prev_mode = None
        if prev_mode_raw:
            try:
                prev_mode = Mode(prev_mode_raw)
            except ValueError:
                logger.warning(
                    "risk_state_invalid_prev_mode",
                    extra={"extra": {"current_mode_raw": prev_mode_raw}},
                )

        today = self.now_provider().date()
        peak_equity = self._resolve_peak_equity(current, pnl_report.equity_estimate)
        drawdown = max(Decimal("0"), peak_equity - pnl_report.equity_estimate)
        gross_exposure = self._compute_gross_exposure(positions, mark_prices)
        largest_position_pct = self._compute_largest_position_pct(
            positions, mark_prices, pnl_report.equity_estimate
        )
        fees_today = self._resolve_fees_today(pnl_report)

        signals = RiskSignals(
            equity_try=pnl_report.equity_estimate,
            peak_equity_try=peak_equity,
            drawdown_try=drawdown,
            daily_pnl_try=realized_today_try,
            gross_exposure_try=gross_exposure,
            largest_position_pct=largest_position_pct,
            fees_try_today=fees_today,
        )
        mode, reasons = decide_mode(limits=limits, signals=signals)
        decided_at = self.now_provider()
        if kill_switch_active:
            mode = Mode.OBSERVE_ONLY
            reasons = ["KILL_SWITCH"]

        risk_decision = RiskDecision(
            mode=mode,
            reasons=reasons,
            limits=limits,
            signals=signals,
            decided_at=decided_at,
        )
        accounting = PortfolioAccountingState(
            as_of=decided_at,
            balances_try={"TRY": pnl_report.equity_estimate},
            locked_try={},
            treasury_try=Decimal("0"),
            trading_capital_try=pnl_report.equity_estimate,
            realized_pnl_try=Decimal("0"),
            unrealized_pnl_try=Decimal("0"),
            fees_try=Decimal("0"),
            funding_cost_try=Decimal("0"),
            slippage_try=Decimal("0"),
            symbols={},
        )
        budget_view = self.budget_policy.evaluate(
            accounting=accounting,
            peak_equity_try=peak_equity,
            realized_pnl_today_try=realized_today_try,
            consecutive_loss_streak=0,
            volatility_regime="normal",
        )
        budget_multiplier = budget_view.position_sizing_multiplier
        if mode == Mode.OBSERVE_ONLY:
            budget_multiplier = Decimal("0")
        elif mode == Mode.REDUCE_RISK_ONLY:
            budget_multiplier = min(budget_multiplier, Decimal("0.5"))

        decision = BudgetDecision(
            risk_decision=risk_decision,
            budget_view=RiskBudgetView(
                trading_capital_try=budget_view.trading_capital_try,
                treasury_try=budget_view.treasury_try,
                available_risk_capital_try=budget_view.available_risk_capital_try,
                daily_loss_limit_try=budget_view.daily_loss_limit_try,
                drawdown_halt_limit_try=budget_view.drawdown_halt_limit_try,
                max_gross_exposure_try=budget_view.max_gross_exposure_try,
                max_order_notional_try=budget_view.max_order_notional_try,
                position_sizing_multiplier=budget_multiplier,
                mode=mode,
            ),
        )
        return decision, prev_mode, peak_equity, fees_today, today

    def persist_decision(
        self,
        *,
        cycle_id: str,
        decision: BudgetDecision | RiskDecision,
        prev_mode: Mode | None,
        peak_equity: Decimal,
        peak_day: date,
        fees_today: Decimal,
        fees_day: date,
    ) -> None:
        self.state_store.persist_risk(
            cycle_id=cycle_id,
            decision=(decision.risk_decision if isinstance(decision, BudgetDecision) else decision),
            prev_mode=(prev_mode.value if prev_mode else None),
            mode=decision.mode,
            peak_equity_try=peak_equity,
            peak_day=peak_day.isoformat(),
            fees_today_try=fees_today,
            fees_day=fees_day.isoformat(),
        )

    def _resolve_peak_equity(
        self,
        current: dict[str, str | None],
        equity_try: Decimal,
    ) -> Decimal:
        peak_raw = current.get("peak_equity_try")
        if peak_raw is None:
            return equity_try
        return max(Decimal(str(peak_raw)), equity_try)

    def _resolve_fees_today(self, pnl_report: PnlReport) -> Decimal:
        return Decimal(str(pnl_report.fees_total_by_currency.get("TRY", Decimal("0"))))

    def _compute_gross_exposure(
        self,
        positions: list[Position],
        mark_prices: dict[str, Decimal],
    ) -> Decimal:
        total = Decimal("0")
        for position in positions:
            mark = mark_prices.get(position.symbol, position.avg_cost_try)
            total += abs(position.qty * mark)
        return total

    def _compute_largest_position_pct(
        self,
        positions: list[Position],
        mark_prices: dict[str, Decimal],
        equity_try: Decimal,
    ) -> Decimal:
        if equity_try <= 0:
            return Decimal("0")
        largest = Decimal("0")
        for position in positions:
            mark = mark_prices.get(position.symbol, position.avg_cost_try)
            notional = abs(position.qty * mark)
            largest = max(largest, notional)
        return largest / equity_try
