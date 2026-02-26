from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_engine import (
    REASON_DRAWDOWN_LIMIT,
    REASON_EXCHANGE_DEGRADED,
    REASON_FEE_BURN,
    REASON_HIGH_VOL,
    REASON_KILL_SWITCH,
    REASON_LOSS_LIMIT,
    REASON_OK,
    REASON_STALE_DATA,
    CycleRiskOutput,
)
from btcbot.domain.risk_models import ExposureSnapshot, stable_hash_payload


@dataclass(frozen=True)
class Stage7RiskInputs:
    max_drawdown_pct: Decimal
    daily_pnl_try: Decimal
    consecutive_loss_streak: int
    market_data_age_sec: int
    observed_spread_bps: Decimal
    quote_volume_try: Decimal
    exposure_snapshot: ExposureSnapshot
    fee_burn_today_try: Decimal = Decimal("0")
    reject_rate_window: Decimal = Decimal("0")
    exchange_degraded: bool = False
    stale_age_sec: int = 0


class Stage7RiskBudgetService:
    def decide(
        self,
        *,
        settings: Settings,
        now_utc: datetime,
        inputs: Stage7RiskInputs,
        previous_decision: CycleRiskOutput | None,
    ) -> CycleRiskOutput:
        mode = Mode.NORMAL
        reasons: list[str] = []
        cooldown_until: datetime | None = None

        max_order_notional_try = min(
            settings.max_order_notional_try, settings.notional_cap_try_per_cycle
        )
        max_orders_per_cycle = settings.max_orders_per_cycle
        max_symbol_exposure_try = settings.max_symbol_exposure_try

        def _apply_mode(next_mode: Mode, reason: str) -> None:
            nonlocal mode, cooldown_until
            if reason not in reasons:
                reasons.append(reason)
            if next_mode == Mode.OBSERVE_ONLY:
                mode = Mode.OBSERVE_ONLY
                if settings.risk_cooldown_sec > 0:
                    cooldown_until = now_utc.astimezone(UTC) + timedelta(
                        seconds=settings.risk_cooldown_sec
                    )
            elif next_mode == Mode.REDUCE_RISK_ONLY and mode == Mode.NORMAL:
                mode = Mode.REDUCE_RISK_ONLY

        if settings.kill_switch:
            _apply_mode(Mode.OBSERVE_ONLY, REASON_KILL_SWITCH)
        if inputs.exchange_degraded:
            _apply_mode(Mode.OBSERVE_ONLY, REASON_EXCHANGE_DEGRADED)

        stale_age_sec = max(inputs.market_data_age_sec, inputs.stale_age_sec)
        if stale_age_sec > settings.stage7_max_data_age_sec:
            _apply_mode(Mode.OBSERVE_ONLY, REASON_STALE_DATA)

        drawdown_bps = int((inputs.max_drawdown_pct * Decimal("10000")).to_integral_value())
        if inputs.daily_pnl_try <= (settings.daily_loss_limit_try * Decimal("-1")):
            _apply_mode(Mode.REDUCE_RISK_ONLY, REASON_LOSS_LIMIT)
        if drawdown_bps >= settings.max_drawdown_bps:
            _apply_mode(Mode.REDUCE_RISK_ONLY, REASON_DRAWDOWN_LIMIT)
        if inputs.fee_burn_today_try >= settings.fee_burn_limit_try:
            _apply_mode(Mode.REDUCE_RISK_ONLY, REASON_FEE_BURN)

        if inputs.observed_spread_bps >= settings.high_vol_threshold_bps:
            if REASON_HIGH_VOL not in reasons:
                reasons.append(REASON_HIGH_VOL)
            max_order_notional_try *= Decimal("0.5")
            max_orders_per_cycle = max(1, settings.max_orders_per_cycle // 2)
            max_symbol_exposure_try *= Decimal("0.5")
            if inputs.observed_spread_bps >= settings.high_vol_threshold_bps * Decimal("2"):
                _apply_mode(Mode.REDUCE_RISK_ONLY, REASON_HIGH_VOL)

        prev_cooldown = getattr(previous_decision, "cooldown_until_utc", None) or getattr(
            previous_decision, "cooldown_until", None
        )
        if prev_cooldown and prev_cooldown.astimezone(UTC) > now_utc.astimezone(UTC):
            prev_mode = getattr(previous_decision, "mode", Mode.NORMAL)
            if prev_mode == Mode.OBSERVE_ONLY:
                mode = Mode.OBSERVE_ONLY
            elif prev_mode == Mode.REDUCE_RISK_ONLY and mode == Mode.NORMAL:
                mode = Mode.REDUCE_RISK_ONLY
            cooldown_until = prev_cooldown

        if not reasons:
            reasons = [REASON_OK]

        inputs_hash = stable_hash_payload(
            {
                "now_utc": now_utc,
                "inputs": {
                    "max_drawdown_pct": str(inputs.max_drawdown_pct),
                    "daily_pnl_try": str(inputs.daily_pnl_try),
                    "market_data_age_sec": inputs.market_data_age_sec,
                    "observed_spread_bps": str(inputs.observed_spread_bps),
                    "quote_volume_try": str(inputs.quote_volume_try),
                    "fee_burn_today_try": str(inputs.fee_burn_today_try),
                    "reject_rate_window": str(inputs.reject_rate_window),
                    "exchange_degraded": inputs.exchange_degraded,
                    "stale_age_sec": stale_age_sec,
                    "exposure_hash": inputs.exposure_snapshot.inputs_hash,
                },
            }
        )

        return CycleRiskOutput(
            mode=mode,
            reasons=reasons,
            max_order_notional_try=max_order_notional_try,
            max_orders_per_cycle=max_orders_per_cycle,
            max_symbol_exposure_try=max_symbol_exposure_try,
            daily_loss_limit_try=settings.daily_loss_limit_try,
            max_drawdown_bps=settings.max_drawdown_bps,
            fee_burn_limit_try=settings.fee_burn_limit_try,
            cooldown_until_utc=cooldown_until,
            allow_submit=(mode != Mode.OBSERVE_ONLY),
            allow_cancel=True,
            decided_at=now_utc.astimezone(UTC),
            inputs_hash=inputs_hash,
            metrics={
                "loss_today": str(inputs.daily_pnl_try),
                "drawdown_bps": drawdown_bps,
                "fee_burn_today": str(inputs.fee_burn_today_try),
                "reject_rate_window": str(inputs.reject_rate_window),
                "stale_age_sec": stale_age_sec,
            },
        )
