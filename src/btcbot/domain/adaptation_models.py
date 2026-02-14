from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class Stage7Params:
    universe_size: int
    score_weights: dict[str, Decimal]
    order_offset_bps: int
    turnover_cap_try: Decimal
    max_orders_per_cycle: int
    max_spread_bps: int
    cash_target_try: Decimal
    min_quote_volume_try: Decimal
    version: int
    updated_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "universe_size": self.universe_size,
            "score_weights": {key: str(value) for key, value in sorted(self.score_weights.items())},
            "order_offset_bps": self.order_offset_bps,
            "turnover_cap_try": str(self.turnover_cap_try),
            "max_orders_per_cycle": self.max_orders_per_cycle,
            "max_spread_bps": self.max_spread_bps,
            "cash_target_try": str(self.cash_target_try),
            "min_quote_volume_try": str(self.min_quote_volume_try),
            "version": self.version,
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Stage7Params:
        return cls(
            universe_size=int(payload["universe_size"]),
            score_weights={
                str(key): Decimal(str(value))
                for key, value in dict(payload.get("score_weights") or {}).items()
            },
            order_offset_bps=int(payload["order_offset_bps"]),
            turnover_cap_try=Decimal(str(payload["turnover_cap_try"])),
            max_orders_per_cycle=int(payload["max_orders_per_cycle"]),
            max_spread_bps=int(payload["max_spread_bps"]),
            cash_target_try=Decimal(str(payload["cash_target_try"])),
            min_quote_volume_try=Decimal(str(payload.get("min_quote_volume_try", "0"))),
            version=int(payload["version"]),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
        )


@dataclass(frozen=True)
class ParamChange:
    change_id: str
    ts: datetime
    from_version: int
    to_version: int
    changes: dict[str, dict[str, str]]
    reason: str
    metrics_window: dict[str, str]
    outcome: Literal["APPLIED", "REJECTED", "ROLLED_BACK"]
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "ts": self.ts.astimezone(UTC).isoformat(),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "changes": self.changes,
            "reason": self.reason,
            "metrics_window": self.metrics_window,
            "outcome": self.outcome,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ParamChange:
        return cls(
            change_id=str(payload["change_id"]),
            ts=datetime.fromisoformat(str(payload["ts"])),
            from_version=int(payload["from_version"]),
            to_version=int(payload["to_version"]),
            changes={
                str(field): {str(k): str(v) for k, v in values.items()}
                for field, values in dict(payload.get("changes") or {}).items()
            },
            reason=str(payload["reason"]),
            metrics_window={
                str(key): str(value)
                for key, value in dict(payload.get("metrics_window") or {}).items()
            },
            outcome=str(payload["outcome"]),
            notes=[str(item) for item in list(payload.get("notes") or [])],
        )
