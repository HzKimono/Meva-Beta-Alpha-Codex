from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
import json
from typing import Protocol

from btcbot.domain.risk_budget import Mode, RiskDecision
from btcbot.domain.risk_mode_codec import dump_risk_mode


class RiskRepoProtocol(Protocol):
    def get_risk_state_current(self) -> dict[str, str | None]: ...

    def save_risk_decision(self, *, cycle_id: str, decision: RiskDecision, prev_mode: Mode | None) -> None: ...

    def upsert_risk_state_current(
        self,
        *,
        risk_mode: Mode,
        peak_equity_try: Decimal,
        peak_equity_date: str,
        fees_try_today: Decimal,
        fees_day: str,
    ) -> None: ...



def serialize_risk_payload(value: object) -> str:
    from btcbot.domain.risk_budget import Mode as RiskMode

    def _json_default(obj: object) -> str:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, RiskMode):
            return dump_risk_mode(obj) or ""
        raise TypeError(f"Unsupported type for risk payload serialization: {type(obj).__name__}")

    payload = asdict(value)
    return json.dumps(payload, sort_keys=True, default=_json_default)
