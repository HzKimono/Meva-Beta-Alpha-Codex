from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from btcbot.domain.risk_budget import Mode, RiskDecision, RiskLimits, RiskSignals, decide_mode
from btcbot.domain.stage4 import Position
from btcbot.services.ledger_service import PnlReport
from btcbot.services.state_store import StateStore


class RiskBudgetService:
    def __init__(
        self,
        state_store: StateStore,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    def compute_decision(
        self,
        *,
        limits: RiskLimits,
        pnl_report: PnlReport,
        positions: list[Position],
        mark_prices: dict[str, Decimal],
        realized_today_try: Decimal,
        kill_switch_active: bool,
    ) -> tuple[RiskDecision, Mode | None, Decimal, Decimal, date]:
        current = self.state_store.get_risk_state_current()
        prev_mode_raw = current.get("current_mode")
        prev_mode = Mode(prev_mode_raw) if prev_mode_raw else None

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

        decision = RiskDecision(
            mode=mode,
            reasons=reasons,
            limits=limits,
            signals=signals,
            decided_at=decided_at,
        )
        return decision, prev_mode, peak_equity, fees_today, today

    def persist_decision(
        self,
        *,
        cycle_id: str,
        decision: RiskDecision,
        prev_mode: Mode | None,
        peak_equity: Decimal,
        peak_day: date,
        fees_today: Decimal,
        fees_day: date,
    ) -> None:
        self.state_store.persist_risk(
            cycle_id=cycle_id,
            decision=decision,
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
