from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: Decimal
    mark_price_try: Decimal
    notional_try: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "qty": str(self.qty),
            "mark_price_try": str(self.mark_price_try),
            "notional_try": str(self.notional_try),
        }


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash_try: Decimal
    positions: list[PositionSnapshot]
    equity_try: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "cash_try": str(self.cash_try),
            "positions": [position.to_dict() for position in self.positions],
            "equity_try": str(self.equity_try),
        }


@dataclass(frozen=True)
class TargetAllocation:
    symbol: str
    weight: Decimal
    target_notional_try: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "weight": str(self.weight),
            "target_notional_try": str(self.target_notional_try),
        }


@dataclass(frozen=True)
class RebalanceAction:
    symbol: str
    side: Literal["BUY", "SELL"]
    target_notional_try: Decimal
    est_qty: Decimal
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "target_notional_try": str(self.target_notional_try),
            "est_qty": str(self.est_qty),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PortfolioPlan:
    timestamp: datetime
    universe: list[str]
    cash_target_try: Decimal
    allocations: list[TargetAllocation]
    actions: list[RebalanceAction]
    constraints_summary: dict[str, str]
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "universe": self.universe,
            "cash_target_try": str(self.cash_target_try),
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "actions": [action.to_dict() for action in self.actions],
            "constraints_summary": self.constraints_summary,
            "notes": self.notes,
        }
