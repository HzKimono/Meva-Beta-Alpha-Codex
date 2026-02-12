from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.models import OrderSide


@dataclass(frozen=True)
class TradeFill:
    fill_id: str | None
    order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_currency: str
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Position:
    symbol: str
    qty: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
