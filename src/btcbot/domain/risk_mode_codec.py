from __future__ import annotations

import logging

from btcbot.domain.risk_budget import Mode

logger = logging.getLogger(__name__)

_RISK_MODE_ALIASES: dict[str, Mode] = {
    Mode.NORMAL.value: Mode.NORMAL,
    "NORMAL_MODE": Mode.NORMAL,
    Mode.REDUCE_RISK_ONLY.value: Mode.REDUCE_RISK_ONLY,
    "REDUCE_ONLY": Mode.REDUCE_RISK_ONLY,
    "REDUCE_RISK": Mode.REDUCE_RISK_ONLY,
    Mode.OBSERVE_ONLY.value: Mode.OBSERVE_ONLY,
    "OBSERVE": Mode.OBSERVE_ONLY,
}


def parse_risk_mode(value: str | None) -> Mode | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not normalized:
        return None
    mode = _RISK_MODE_ALIASES.get(normalized)
    if mode is not None:
        return mode
    logger.warning("risk_mode_codec_unknown_mode", extra={"extra": {"raw_mode": value}})
    return None


def dump_risk_mode(mode: Mode | None) -> str | None:
    if mode is None:
        return None
    return mode.value
