from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class SizedAction:
    symbol: str
    side: Literal["buy", "sell"]
    notional_try: Decimal
    qty: Decimal
    rationale: str
    strategy_id: str


@dataclass(frozen=True)
class AllocationDecision:
    symbol: str
    side: Literal["buy", "sell"]
    intent_type: Literal["place", "cancel", "replace"]
    requested_notional_try: Decimal | None
    allocated_notional_try: Decimal | None
    allocated_qty: Decimal | None
    status: Literal["accepted", "scaled", "rejected"]
    reason: str
    strategy_id: str


@dataclass(frozen=True)
class AllocationResult:
    actions: list[SizedAction]
    decisions: list[AllocationDecision]
    counters: dict[str, int]
