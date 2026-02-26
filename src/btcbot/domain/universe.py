from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True)
class UniverseCandidate:
    symbol: str
    liquidity_score: Decimal
    spread_score: Decimal
    volatility_score: Decimal
    total_score: Decimal
    breakdown: dict[str, float | int | str]


ScoredSymbolCandidate = UniverseCandidate


@dataclass(frozen=True)
class UniverseSelectionResult:
    selected_symbols: list[str]
    scored: list[UniverseCandidate]
    reasons: list[str]
    timestamp: datetime
    freeze_reason: str | None = None
    freeze_reasons: list[str] | None = None
    excluded_counts: dict[str, int] | None = None
    churn_count: int = 0

    def __post_init__(self) -> None:
        if self.freeze_reasons is None:
            object.__setattr__(self, "freeze_reasons", [])
        if self.excluded_counts is None:
            object.__setattr__(self, "excluded_counts", {})
        if self.freeze_reason is None and self.freeze_reasons:
            object.__setattr__(self, "freeze_reason", self.freeze_reasons[0])

    @property
    def ts_utc(self) -> datetime:
        return self.timestamp.astimezone(UTC)
