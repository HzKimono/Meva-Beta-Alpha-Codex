from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Mode(Enum):
    NORMAL = "NORMAL"
    REDUCE_RISK_ONLY = "REDUCE_RISK_ONLY"
    OBSERVE_ONLY = "OBSERVE_ONLY"


REASON_DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
REASON_EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
REASON_FEE_BUDGET = "FEE_BUDGET"
REASON_OK = "OK"


@dataclass(frozen=True)
class RiskLimits:
    max_daily_drawdown_try: Decimal
    max_drawdown_try: Decimal
    max_gross_exposure_try: Decimal
    max_position_pct: Decimal
    max_order_notional_try: Decimal
    min_cash_try: Decimal | None = None
    max_fee_try_per_day: Decimal | None = None


@dataclass(frozen=True)
class RiskSignals:
    equity_try: Decimal
    peak_equity_try: Decimal
    drawdown_try: Decimal
    daily_pnl_try: Decimal
    gross_exposure_try: Decimal
    largest_position_pct: Decimal
    fees_try_today: Decimal
    current_equity_try: Decimal | None = None
    realized_today_try: Decimal | None = None
    exposure_try: Decimal | None = None
    volatility_state: str | None = None
    loss_streak: int | None = None
    missing_mark_prices: tuple[str, ...] = ()
    missing_currencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    mode: Mode
    reasons: list[str]
    limits: RiskLimits
    signals: RiskSignals
    decided_at: datetime


def decide_mode(limits: RiskLimits, signals: RiskSignals) -> tuple[Mode, list[str]]:
    if (
        signals.drawdown_try >= limits.max_drawdown_try
        or signals.daily_pnl_try <= -limits.max_daily_drawdown_try
    ):
        return Mode.OBSERVE_ONLY, [REASON_DRAWDOWN_LIMIT]
    if (
        signals.gross_exposure_try > limits.max_gross_exposure_try
        or signals.largest_position_pct > limits.max_position_pct
    ):
        return Mode.REDUCE_RISK_ONLY, [REASON_EXPOSURE_LIMIT]
    if (
        limits.max_fee_try_per_day is not None
        and signals.fees_try_today > limits.max_fee_try_per_day
    ):
        return Mode.REDUCE_RISK_ONLY, [REASON_FEE_BUDGET]
    return Mode.NORMAL, [REASON_OK]
