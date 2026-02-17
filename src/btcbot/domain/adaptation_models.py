from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast


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
        score_weights_raw = payload.get("score_weights")
        score_weights_map: Mapping[object, object]
        if isinstance(score_weights_raw, Mapping):
            score_weights_map = score_weights_raw
        else:
            score_weights_map = {}
        return cls(
            universe_size=int(str(payload["universe_size"])),
            score_weights={
                str(key): Decimal(str(value)) for key, value in score_weights_map.items()
            },
            order_offset_bps=int(str(payload["order_offset_bps"])),
            turnover_cap_try=Decimal(str(payload["turnover_cap_try"])),
            max_orders_per_cycle=int(str(payload["max_orders_per_cycle"])),
            max_spread_bps=int(str(payload["max_spread_bps"])),
            cash_target_try=Decimal(str(payload["cash_target_try"])),
            min_quote_volume_try=Decimal(str(payload.get("min_quote_volume_try", "0"))),
            version=int(str(payload["version"])),
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
        changes_raw = payload.get("changes")
        changes_map: Mapping[object, object]
        if isinstance(changes_raw, Mapping):
            changes_map = changes_raw
        else:
            changes_map = {}

        metrics_raw = payload.get("metrics_window")
        metrics_map: Mapping[object, object]
        if isinstance(metrics_raw, Mapping):
            metrics_map = metrics_raw
        else:
            metrics_map = {}

        notes_raw = payload.get("notes")
        notes_list: list[Any]
        if isinstance(notes_raw, list):
            notes_list = notes_raw
        else:
            notes_list = []

        return cls(
            change_id=str(payload["change_id"]),
            ts=datetime.fromisoformat(str(payload["ts"])),
            from_version=int(str(payload["from_version"])),
            to_version=int(str(payload["to_version"])),
            changes={
                str(field): {str(k): str(v) for k, v in values.items()}
                for field, values in changes_map.items()
                if isinstance(values, Mapping)
            },
            reason=str(payload["reason"]),
            metrics_window={str(key): str(value) for key, value in metrics_map.items()},
            outcome=cast(
                Literal["APPLIED", "REJECTED", "ROLLED_BACK"],
                str(payload["outcome"]),
            ),
            notes=[str(item) for item in notes_list],
        )
