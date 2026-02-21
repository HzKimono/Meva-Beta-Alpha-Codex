from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class UnknownOrderRecord:
    order_id: str
    reason: str
    ts_ms: int


class UnknownOrderRegistry:
    """Tracks uncertain orders and exposes freeze state for submit gating."""

    def __init__(self) -> None:
        self._records: dict[str, UnknownOrderRecord] = {}
        self._frozen_since_monotonic: float | None = None

    def mark_unknown(self, order_id: str, reason: str, ts: int) -> None:
        if order_id not in self._records and not self._records:
            self._frozen_since_monotonic = time.monotonic()
        self._records[order_id] = UnknownOrderRecord(order_id=order_id, reason=reason, ts_ms=ts)

    def mark_resolved(self, order_id: str) -> None:
        self._records.pop(order_id, None)
        if not self._records:
            self._frozen_since_monotonic = None

    def has_unknown(self) -> bool:
        return bool(self._records)

    def count_unknown(self) -> int:
        return len(self._records)

    def frozen_seconds(self) -> float:
        if self._frozen_since_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._frozen_since_monotonic)

    def snapshot(self) -> list[UnknownOrderRecord]:
        return sorted(self._records.values(), key=lambda record: record.order_id)

    def sync_snapshot(self, records: list[UnknownOrderRecord], *, allow_clear: bool) -> None:
        incoming = {record.order_id: record for record in records}
        if allow_clear:
            had_unknown = bool(self._records)
            self._records = incoming
            if not incoming:
                self._frozen_since_monotonic = None
            elif not had_unknown or self._frozen_since_monotonic is None:
                self._frozen_since_monotonic = time.monotonic()
            return

        for record in records:
            self._records[record.order_id] = record
        if self._records and self._frozen_since_monotonic is None:
            self._frozen_since_monotonic = time.monotonic()
