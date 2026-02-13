from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal

from btcbot.domain.risk_budget import Mode, RiskDecision, RiskLimits, RiskSignals, decide_mode
from btcbot.domain.stage4 import Fill, Position
from btcbot.services.ledger_service import PnlReport
from btcbot.services.state_store import StateStore


class RiskBudgetService:
    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store

    def compute_decision(
        self,
        *,
        limits: RiskLimits,
        pnl_report: PnlReport,
        positions: list[Position],
        mark_prices: dict[str, Decimal],
        realized_today_try: Decimal,
        fills: list[Fill],
        kill_switch_active: bool,
    ) -> tuple[RiskDecision, Mode | None, Decimal, Decimal, date]:
        current = self.state_store.get_risk_state_current()
        prev_mode_raw = current.get("current_mode")
        prev_mode = Mode(prev_mode_raw) if prev_mode_raw else None

        today = datetime.now(UTC).date()
        peak_equity = self._resolve_peak_equity(current, pnl_report.equity_estimate, today)
        drawdown = max(Decimal("0"), peak_equity - pnl_report.equity_estimate)
        gross_exposure = self._compute_gross_exposure(positions, mark_prices)
        largest_position_pct = self._compute_largest_position_pct(
            positions, mark_prices, pnl_report.equity_estimate
        )
        fees_today = self._resolve_fees_today(current, pnl_report, fills, today)

        signals = RiskSignals(
            equity_try=pnl_report.equity_estimate,
            peak_equity_try=peak_equity,
            drawdown_try=drawdown,
            daily_pnl_try=realized_today_try,
            gross_exposure_try=gross_exposure,
            largest_position_pct=largest_position_pct,
            fees_try_today=fees_today,
        )
        decision = decide_mode(limits=limits, signals=signals)
        if kill_switch_active:
            decision = RiskDecision(
                mode=Mode.OBSERVE_ONLY,
                reasons=["KILL_SWITCH"],
                limits=decision.limits,
                signals=decision.signals,
                decided_at=decision.decided_at,
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
        self.state_store.save_risk_decision(
            cycle_id=cycle_id,
            decision=decision,
            prev_mode=(prev_mode.value if prev_mode else None),
        )
        self.state_store.upsert_risk_state_current(
            mode=decision.mode.value,
            peak_equity_try=peak_equity,
            peak_equity_date=peak_day.isoformat(),
            fees_try_today=fees_today,
            fees_day=fees_day.isoformat(),
        )

    def _resolve_peak_equity(
        self,
        current: dict[str, str | None],
        equity_try: Decimal,
        today: date,
    ) -> Decimal:
        peak_raw = current.get("peak_equity_try")
        if peak_raw is None:
            peak = equity_try
        else:
            peak = Decimal(str(peak_raw))
        peak_day = current.get("peak_equity_date")
        if peak_day != today.isoformat():
            return equity_try
        return max(peak, equity_try)

    def _resolve_fees_today(
        self,
        current: dict[str, str | None],
        pnl_report: PnlReport,
        fills: list[Fill],
        today: date,
    ) -> Decimal:
        fees_day = current.get("fees_day")
        stored = (
            Decimal(str(current["fees_try_today"]))
            if fees_day == today.isoformat() and current.get("fees_try_today") is not None
            else Decimal("0")
        )
        fill_fees = sum(
            (fill.fee for fill in fills if str(fill.fee_asset).upper() == "TRY"),
            Decimal("0"),
        )
        ledger_total_try = Decimal(str(pnl_report.fees_total_by_currency.get("TRY", Decimal("0"))))
        if fill_fees > 0:
            return stored + fill_fees
        return max(stored, ledger_total_try)

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


def serialize_dataclass_payload(value: object) -> str:
    payload = asdict(value)
    return json.dumps(payload, sort_keys=True, default=_json_default)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mode):
        return value.value
    raise TypeError(f"Unsupported type for risk payload serialization: {type(value).__name__}")
