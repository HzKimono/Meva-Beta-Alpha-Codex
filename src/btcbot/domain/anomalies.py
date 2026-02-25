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
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


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
    level: int = 0
    shrink_notional_factor: float = 1.0
    universe_cap: int | None = None
    recovery_streak: int = 0


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
    recent_warn_codes: set[AnomalyCode],
    previous_level: int = 0,
    breaker_open: bool = False,
    freeze_active: bool = False,
    stability_streak: int = 0,
) -> DegradeDecision:
    # Backward-compatible rule: empty/None warn_codes means all WARN codes are allowed.
    allow_all_warn_codes = not warn_codes
    allowed_warn_codes = set(warn_codes or set())

    filtered_current_warn_codes = {
        event.code
        for event in anomalies
        if event.severity == "WARN"
        and (allow_all_warn_codes or event.code in allowed_warn_codes)
    }
    filtered_recent_warn_codes = {
        code for code in recent_warn_codes if allow_all_warn_codes or code in allowed_warn_codes
    }

    def _level_policy(level: int) -> tuple[Mode | None, float, int | None, int]:
        if level >= 3:
            return Mode.OBSERVE_ONLY, 0.0, 2, 1800
        if level == 2:
            return Mode.REDUCE_RISK_ONLY, 0.25, 4, 900
        if level == 1:
            return Mode.REDUCE_RISK_ONLY, 0.50, 8, 900
        return None, 1.0, None, 0

    def _build(level: int, reasons: list[str], recovery: int) -> DegradeDecision:
        mode, factor, universe_cap, cooldown_seconds = _level_policy(level)
        return DegradeDecision(
            mode_override=mode,
            reasons=reasons,
            cooldown_until=(now + timedelta(seconds=cooldown_seconds) if level >= 1 else None),
            level=level,
            shrink_notional_factor=factor,
            universe_cap=universe_cap,
            recovery_streak=recovery,
        )

    def _level_from_override(override: Mode | None) -> int:
        if override == Mode.OBSERVE_ONLY:
            return 3
        if override == Mode.REDUCE_RISK_ONLY:
            return 1
        return 0

    if cooldown_until is not None and now < cooldown_until and previous_level >= 1:
        return _build(previous_level, list(last_reasons or []), stability_streak)

    if cooldown_until is not None and now < cooldown_until:
        return _build(_level_from_override(current_override), list(last_reasons or []), stability_streak)

    sorted_codes = sorted({event.code.value for event in anomalies})
    if breaker_open or freeze_active or any(event.severity == "ERROR" for event in anomalies):
        critical_reasons = sorted_codes
        if breaker_open:
            critical_reasons.append("BREAKER_OPEN")
        if freeze_active:
            critical_reasons.append("STAGE4_FREEZE_ACTIVE")
        return _build(3, sorted(set(critical_reasons)), 0)

    warn_count_for_threshold = recent_warn_count
    if not filtered_current_warn_codes and not filtered_recent_warn_codes:
        warn_count_for_threshold = 0

    if warn_count_for_threshold >= warn_threshold * 2:
        return _build(2, sorted(code.value for code in filtered_recent_warn_codes), 0)

    if warn_count_for_threshold >= warn_threshold:
        warn_reason_codes = sorted(code.value for code in filtered_recent_warn_codes)
        if not warn_reason_codes:
            warn_reason_codes = list(last_reasons or [])
        return _build(1, warn_reason_codes, 0)

    # Hysteresis: step down only after enough stable cycles.
    if previous_level >= 1:
        next_stability = stability_streak + 1
        required = 8 if previous_level == 1 else 4
        if next_stability < required:
            return _build(previous_level, list(last_reasons or []), next_stability)
        return _build(previous_level - 1, [], 0)

    return _build(0, [], 0)
