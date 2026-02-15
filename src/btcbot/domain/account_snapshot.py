from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType


@dataclass(frozen=True)
class Holding:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True)
class AccountSnapshot:
    timestamp: datetime
    exchange: str
    cash_try: Decimal
    holdings: dict[str, Holding]
    total_equity_try: Decimal
    source_endpoints: tuple[str, ...]
    flags: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "holdings", MappingProxyType(dict(sorted(self.holdings.items()))))
        object.__setattr__(self, "source_endpoints", tuple(dict.fromkeys(self.source_endpoints)))
        object.__setattr__(self, "flags", tuple(sorted(set(self.flags))))
