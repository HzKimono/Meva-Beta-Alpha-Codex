from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class OrderIntent:
    cycle_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["LIMIT"]
    price_try: Decimal
    qty: Decimal
    notional_try: Decimal
    client_order_id: str
    reason: str
    constraints_applied: dict[str, str]
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "price_try": str(self.price_try),
            "qty": str(self.qty),
            "notional_try": str(self.notional_try),
            "client_order_id": self.client_order_id,
            "reason": self.reason,
            "constraints_applied": self.constraints_applied,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }
