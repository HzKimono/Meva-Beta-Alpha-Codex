from __future__ import annotations

from typing import Protocol


class TraceRepoProtocol(Protocol):
    def record_cycle_audit(
        self,
        cycle_id: str,
        counts: dict[str, int],
        decisions: list[str],
        envelope: dict[str, object] | None = None,
    ) -> None: ...
