from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum


class RiskMode(StrEnum):
    NORMAL = "NORMAL"
    REDUCE_RISK_ONLY = "REDUCE_RISK_ONLY"
    OBSERVE_ONLY = "OBSERVE_ONLY"


@dataclass(frozen=True)
class RiskDecision:
    mode: RiskMode
    reasons: dict[str, object]
    cooldown_until: datetime | None
    decided_at: datetime
    inputs_hash: str


@dataclass(frozen=True)
class ExposureSnapshot:
    per_symbol_exposure_try: dict[str, Decimal]
    total_exposure_try: Decimal
    concentration_top_n: list[tuple[str, Decimal]]
    turnover_estimate_try: Decimal
    free_cash_try: Decimal
    computed_at: datetime
    inputs_hash: str


_MODE_ORDER = {
    RiskMode.NORMAL: 0,
    RiskMode.REDUCE_RISK_ONLY: 1,
    RiskMode.OBSERVE_ONLY: 2,
}


def combine_risk_modes(a: RiskMode, b: RiskMode) -> RiskMode:
    return a if _MODE_ORDER[a] >= _MODE_ORDER[b] else b


def stable_hash_payload(payload: object) -> str:
    def _default(value: object) -> str:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"Unsupported stable hash payload type: {type(value).__name__}")

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
