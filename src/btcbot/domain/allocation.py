from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

ReasonCode = Literal[
    "ok",
    "not_implemented_in_pr3",
    "no_mark_price",
    "cash_target",
    "min_notional",
    "max_intent_cap",
    "cycle_notional_cap",
    "max_position_exposure_cap",
    "no_position",
    "position_value_cap",
]


@dataclass(frozen=True)
class SizedAction:
    symbol: str
    side: Literal["buy", "sell"]
    notional_try: Decimal
    qty: Decimal
    rationale: str
    strategy_id: str
    intent_index: int


@dataclass(frozen=True)
class AllocationDecision:
    symbol: str
    side: Literal["buy", "sell"]
    intent_type: Literal["place", "cancel", "replace"]
    requested_notional_try: Decimal | None
    allocated_notional_try: Decimal | None
    allocated_qty: Decimal | None
    status: Literal["accepted", "scaled", "rejected"]
    reason: ReasonCode
    strategy_id: str
    intent_index: int


@dataclass(frozen=True)
class AllocationResult:
    actions: tuple[SizedAction, ...]
    decisions: tuple[AllocationDecision, ...]
    counters: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(
            self,
            "counters",
            MappingProxyType(dict(sorted(dict(self.counters).items()))),
        )
