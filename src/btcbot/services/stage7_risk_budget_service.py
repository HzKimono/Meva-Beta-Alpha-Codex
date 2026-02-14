from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.risk_models import (
    ExposureSnapshot,
    RiskDecision,
    RiskMode,
    combine_risk_modes,
    stable_hash_payload,
)


@dataclass(frozen=True)
class Stage7RiskInputs:
    max_drawdown_pct: Decimal
    daily_pnl_try: Decimal
    consecutive_loss_streak: int
    market_data_age_sec: int
    observed_spread_bps: Decimal
    quote_volume_try: Decimal
    exposure_snapshot: ExposureSnapshot


class Stage7RiskBudgetService:
    def decide(
        self,
        *,
        settings: Settings,
        now_utc: datetime,
        inputs: Stage7RiskInputs,
        previous_decision: RiskDecision | None,
    ) -> RiskDecision:
        mode = RiskMode.NORMAL
        reasons: dict[str, object] = {"rules": []}
        cooldown_until: datetime | None = None

        def _set_mode(next_mode: RiskMode, reason: str, payload: dict[str, object]) -> None:
            nonlocal mode, cooldown_until
            mode = combine_risk_modes(mode, next_mode)
            reasons["rules"].append({"code": reason, **payload})
            if next_mode == RiskMode.OBSERVE_ONLY and settings.stage7_risk_cooldown_sec > 0:
                cooldown_until = now_utc.astimezone(UTC) + timedelta(
                    seconds=settings.stage7_risk_cooldown_sec
                )

        if inputs.max_drawdown_pct >= settings.stage7_max_drawdown_pct:
            _set_mode(
                RiskMode.OBSERVE_ONLY,
                "max_drawdown_breach",
                {
                    "max_drawdown_pct": str(inputs.max_drawdown_pct),
                    "limit": str(settings.stage7_max_drawdown_pct),
                },
            )

        if inputs.daily_pnl_try <= -settings.stage7_max_daily_loss_try:
            _set_mode(
                RiskMode.OBSERVE_ONLY,
                "daily_loss_breach",
                {
                    "daily_pnl_try": str(inputs.daily_pnl_try),
                    "limit": str(settings.stage7_max_daily_loss_try),
                },
            )

        if inputs.consecutive_loss_streak >= settings.stage7_max_consecutive_losses:
            loss_mode = (
                RiskMode.OBSERVE_ONLY
                if settings.stage7_loss_guardrail_mode == "observe_only"
                else RiskMode.REDUCE_RISK_ONLY
            )
            _set_mode(
                loss_mode,
                "consecutive_loss_guardrail",
                {
                    "loss_streak": inputs.consecutive_loss_streak,
                    "limit": settings.stage7_max_consecutive_losses,
                },
            )

        if inputs.market_data_age_sec > settings.stage7_max_data_age_sec:
            _set_mode(
                RiskMode.OBSERVE_ONLY,
                "stale_market_data",
                {
                    "market_data_age_sec": inputs.market_data_age_sec,
                    "limit": settings.stage7_max_data_age_sec,
                },
            )

        if (
            inputs.observed_spread_bps > Decimal(settings.stage7_spread_spike_bps)
            or inputs.quote_volume_try < settings.stage7_min_quote_volume_try
        ):
            _set_mode(
                RiskMode.REDUCE_RISK_ONLY,
                "market_liquidity_guardrail",
                {
                    "observed_spread_bps": str(inputs.observed_spread_bps),
                    "spread_limit_bps": settings.stage7_spread_spike_bps,
                    "quote_volume_try": str(inputs.quote_volume_try),
                    "quote_volume_min_try": str(settings.stage7_min_quote_volume_try),
                },
            )

        if previous_decision and previous_decision.cooldown_until:
            if previous_decision.cooldown_until.astimezone(UTC) > now_utc.astimezone(UTC):
                mode = combine_risk_modes(mode, previous_decision.mode)
                cooldown_until = previous_decision.cooldown_until
                reasons["cooldown_enforced"] = True

        if not reasons["rules"]:
            reasons["rules"].append({"code": "ok"})

        inputs_hash = stable_hash_payload(
            {
                "inputs": {
                    "max_drawdown_pct": str(inputs.max_drawdown_pct),
                    "daily_pnl_try": str(inputs.daily_pnl_try),
                    "consecutive_loss_streak": inputs.consecutive_loss_streak,
                    "market_data_age_sec": inputs.market_data_age_sec,
                    "observed_spread_bps": str(inputs.observed_spread_bps),
                    "quote_volume_try": str(inputs.quote_volume_try),
                    "exposure_hash": inputs.exposure_snapshot.inputs_hash,
                },
                "previous": {
                    "mode": previous_decision.mode.value,
                    "cooldown_until": previous_decision.cooldown_until,
                }
                if previous_decision
                else None,
            }
        )
        return RiskDecision(
            mode=mode,
            reasons=reasons,
            cooldown_until=cooldown_until,
            decided_at=now_utc.astimezone(UTC),
            inputs_hash=inputs_hash,
        )

