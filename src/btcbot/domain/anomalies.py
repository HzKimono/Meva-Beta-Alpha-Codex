from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from btcbot.domain.risk_budget import Mode


class AnomalyCode(Enum):
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    EXCHANGE_LATENCY_SPIKE = "EXCHANGE_LATENCY_SPIKE"
    ORDER_REJECT_SPIKE = "ORDER_REJECT_SPIKE"
    CLOCK_SKEW = "CLOCK_SKEW"
    CURSOR_STALL = "CURSOR_STALL"
    PNL_DIVERGENCE = "PNL_DIVERGENCE"


Severity = Literal["WARN", "ERROR"]


@dataclass(frozen=True)
class AnomalyEvent:
    code: AnomalyCode
    severity: Severity
    ts: datetime
    details: dict[str, str]


@dataclass(frozen=True)
class DegradeDecision:
    mode_override: Mode | None
    reasons: list[str]
    cooldown_until: datetime | None


def combine_modes(base_mode: Mode, override: Mode | None) -> Mode:
    if override is None:
        return base_mode
    if override == Mode.OBSERVE_ONLY:
        return Mode.OBSERVE_ONLY
    if override == Mode.REDUCE_RISK_ONLY and base_mode == Mode.NORMAL:
        return Mode.REDUCE_RISK_ONLY
    return base_mode


def decide_degrade(
    *,
    anomalies: list[AnomalyEvent],
    now: datetime,
    current_override: Mode | None,
    cooldown_until: datetime | None,
    last_reasons: list[str] | None,
    recent_warn_count: int,
    warn_threshold: int,
    warn_codes: set[AnomalyCode],
) -> DegradeDecision:
    if cooldown_until is not None and now < cooldown_until:
        return DegradeDecision(
            mode_override=current_override,
            reasons=list(last_reasons or []),
            cooldown_until=cooldown_until,
        )

    sorted_codes = sorted({event.code.value for event in anomalies})
    if any(event.severity == "ERROR" for event in anomalies):
        return DegradeDecision(
            mode_override=Mode.OBSERVE_ONLY,
            reasons=sorted_codes,
            cooldown_until=now + timedelta(minutes=30),
        )

    if recent_warn_count >= warn_threshold:
        warn_reason_codes = sorted(
            {
                event.code.value
                for event in anomalies
                if event.severity == "WARN" and event.code in warn_codes
            }
        )
        return DegradeDecision(
            mode_override=Mode.REDUCE_RISK_ONLY,
            reasons=warn_reason_codes,
            cooldown_until=now + timedelta(minutes=15),
        )

    return DegradeDecision(mode_override=None, reasons=[], cooldown_until=None)
