from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from btcbot.accounting.models import PortfolioAccountingState
from btcbot.domain.risk_budget import Mode, RiskDecision, RiskLimits, RiskSignals, decide_mode
from btcbot.domain.stage4 import Position
from btcbot.observability_decisions import emit_decision
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


@dataclass(frozen=True)
class CapitalPolicyResult:
    trading_capital_try: Decimal
    treasury_try: Decimal
    realized_pnl_delta_try: Decimal
    checkpoint_id: str
    applied: bool


class CapitalPolicyError(RuntimeError):
    pass


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

    def apply_self_financing_checkpoint(
        self,
        *,
        cycle_id: str,
        realized_pnl_total_try: Decimal,
        ledger_event_count: int,
        ledger_checkpoint_id: str,
        seed_trading_capital_try: Decimal,
    ) -> CapitalPolicyResult:
        state = self._load_or_init_capital_state(
            cycle_id=cycle_id,
            seed_trading_capital_try=seed_trading_capital_try,
            seed_realized_total_try=Decimal("0"),
        )
        trading_capital = Decimal(str(state["trading_capital_try"]))
        treasury = Decimal(str(state["treasury_try"]))
        last_realized_total = Decimal(str(state["last_realized_pnl_total_try"]))
        last_event_count = int(state.get("last_event_count", 0) or 0)

        if ledger_event_count < last_event_count:
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "capital_policy",
                    "reason_code": "capital_error:non_monotonic_checkpoint",
                    "action": "BLOCK",
                    "scope": "global",
                    "payload": {
                        "checkpoint_id": ledger_checkpoint_id,
                        "last_event_count": last_event_count,
                        "ledger_event_count": ledger_event_count,
                    },
                },
            )
            raise CapitalPolicyError(
                "non_monotonic_ledger_checkpoint "
                f"last_event_count={last_event_count} "
                f"ledger_event_count={ledger_event_count}"
            )

        if ledger_event_count == last_event_count:
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "capital_policy",
                    "reason_code": "capital_apply:checkpoint_already_applied",
                    "action": "SUPPRESS",
                    "scope": "global",
                    "payload": {
                        "checkpoint_id": ledger_checkpoint_id,
                        "ledger_event_count": ledger_event_count,
                        "realized_pnl_total_try": str(realized_pnl_total_try),
                    },
                },
            )
            return CapitalPolicyResult(
                trading_capital_try=trading_capital,
                treasury_try=treasury,
                realized_pnl_delta_try=Decimal("0"),
                checkpoint_id=ledger_checkpoint_id,
                applied=False,
            )

        realized_delta = realized_pnl_total_try - last_realized_total
        next_trading, next_treasury = self.budget_policy.apply_self_financing(
            trading_capital_try=trading_capital,
            treasury_try=treasury,
            realized_pnl_delta_try=realized_delta,
        )

        if next_trading < 0 or next_treasury < 0:
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "capital_policy",
                    "reason_code": "capital_error:negative_capital",
                    "action": "BLOCK",
                    "scope": "global",
                    "payload": {
                        "checkpoint_id": ledger_checkpoint_id,
                        "ledger_event_count": ledger_event_count,
                        "realized_pnl_delta_try": str(realized_delta),
                        "trading_capital_try_next": str(next_trading),
                        "treasury_try_next": str(next_treasury),
                    },
                },
            )
            raise CapitalPolicyError(
                "negative_capital_state "
                f"trading_capital_try_next={next_trading} treasury_try_next={next_treasury}"
            )

        self.state_store.upsert_capital_policy_state(
            trading_capital_try=next_trading,
            treasury_try=next_treasury,
            last_realized_pnl_total_try=realized_pnl_total_try,
            last_event_count=ledger_event_count,
            last_checkpoint_id=ledger_checkpoint_id,
            last_cycle_id=cycle_id,
        )

        emit_decision(
            logger,
            {
                "cycle_id": cycle_id,
                "decision_layer": "capital_policy",
                "reason_code": (
                    "capital_apply:split_positive_pnl"
                    if realized_delta > 0
                    else "capital_apply:apply_negative_pnl"
                ),
                "action": "SUBMIT",
                "scope": "global",
                "payload": {
                    "checkpoint_id": ledger_checkpoint_id,
                    "ledger_event_count": ledger_event_count,
                    "realized_pnl_total_try": str(realized_pnl_total_try),
                    "realized_pnl_delta_try": str(realized_delta),
                    "trading_capital_try_prev": str(trading_capital),
                    "treasury_try_prev": str(treasury),
                    "trading_capital_try_next": str(next_trading),
                    "treasury_try_next": str(next_treasury),
                    "deposits_try": "0",
                    "withdrawals_try": "0",
                    "external_costs_try": "0",
                    "treasury_management": "manual_managed",
                },
            },
        )

        return CapitalPolicyResult(
            trading_capital_try=next_trading,
            treasury_try=next_treasury,
            realized_pnl_delta_try=realized_delta,
            checkpoint_id=ledger_checkpoint_id,
            applied=True,
        )

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
        capital_state = self._load_or_init_capital_state(
            cycle_id=f"{today.isoformat()}:compute_decision",
            seed_trading_capital_try=pnl_report.equity_estimate,
            seed_realized_total_try=pnl_report.realized_pnl_total,
        )
        accounting = PortfolioAccountingState(
            as_of=decided_at,
            balances_try={"TRY": Decimal(str(capital_state["trading_capital_try"]))},
            locked_try={},
            treasury_try=Decimal(str(capital_state["treasury_try"])),
            trading_capital_try=Decimal(str(capital_state["trading_capital_try"])),
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

    def _load_or_init_capital_state(
        self,
        *,
        cycle_id: str,
        seed_trading_capital_try: Decimal,
        seed_realized_total_try: Decimal,
    ) -> dict[str, str | int]:
        state = self.state_store.get_capital_policy_state()
        if state is not None:
            return state
        self.state_store.upsert_capital_policy_state(
            trading_capital_try=seed_trading_capital_try,
            treasury_try=Decimal("0"),
            last_realized_pnl_total_try=seed_realized_total_try,
            last_event_count=0,
            last_checkpoint_id=None,
            last_cycle_id=cycle_id,
        )
        persisted = self.state_store.get_capital_policy_state()
        if persisted is not None:
            return persisted
        return {
            "trading_capital_try": str(seed_trading_capital_try),
            "treasury_try": "0",
            "last_realized_pnl_total_try": str(seed_realized_total_try),
            "last_event_count": 0,
            "last_checkpoint_id": None,
            "last_cycle_id": cycle_id,
        }

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
