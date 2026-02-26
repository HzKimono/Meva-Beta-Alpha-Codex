from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from btcbot.domain.risk_budget import Mode

REASON_STALE_DATA = "STALE_DATA"
REASON_EXCHANGE_DEGRADED = "EXCHANGE_DEGRADED"
REASON_LOSS_LIMIT = "LOSS_LIMIT"
REASON_DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
REASON_FEE_BURN = "FEE_BURN"
REASON_HIGH_VOL = "HIGH_VOL"
REASON_NOT_ARMED = "NOT_ARMED"
REASON_KILL_SWITCH = "KILL_SWITCH"
REASON_OK = "OK"


@dataclass(frozen=True)
class CycleRiskOutput:
    mode: Mode
    reasons: list[str]
    max_order_notional_try: Decimal
    max_orders_per_cycle: int
    max_symbol_exposure_try: Decimal
    daily_loss_limit_try: Decimal
    max_drawdown_bps: int
    fee_burn_limit_try: Decimal
    cooldown_until_utc: datetime | None
    allow_submit: bool
    allow_cancel: bool
    decided_at: datetime
    inputs_hash: str
    metrics: dict[str, object] = field(default_factory=dict)

    @property
    def cooldown_until(self) -> datetime | None:
        return self.cooldown_until_utc
