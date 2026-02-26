from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


ScoredSymbolCandidate = UniverseCandidate


@dataclass(frozen=True)
class UniverseSelectionResult:
    selected_symbols: list[str]
    scored: list[UniverseCandidate]
    reasons: list[str]
    timestamp: datetime

    @property
    def ts_utc(self) -> datetime:
        return self.timestamp
