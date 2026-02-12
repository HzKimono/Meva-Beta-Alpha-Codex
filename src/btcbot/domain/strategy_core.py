from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from btcbot.domain.models import normalize_symbol


class Signal(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class OrderBookSummary:
    best_bid: Decimal
    best_ask: Decimal


@dataclass(frozen=True)
class PositionSummary:
    symbol: str
    qty: Decimal
    avg_cost: Decimal


@dataclass(frozen=True)
class OpenOrdersSummary:
    buy_count: int = 0
    sell_count: int = 0


@dataclass(frozen=True)
class StrategyKnobs:
    anchor_price: Decimal | None = None
    mean_reversion_bps: Decimal = Decimal("50")
    max_notional_try: Decimal = Decimal("100")
    bootstrap_notional_try: Decimal = Decimal("50")


@dataclass(frozen=True)
class StrategyContext:
    timestamp: datetime
    symbol: str
    mark_price: Decimal
    orderbook: OrderBookSummary
    balances: dict[str, Decimal]
    position: PositionSummary | None
    open_orders: OpenOrdersSummary
    knobs: StrategyKnobs

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: Literal["buy", "sell"]
    intent_type: Literal["place", "cancel", "replace"]
    target_notional_try: Decimal
    rationale: str
    strategy_id: str
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if self.target_notional_try <= Decimal("0"):
            raise ValueError("target_notional_try must be > 0")
        if self.confidence is None:
            return
        if self.confidence < Decimal("0") or self.confidence > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
