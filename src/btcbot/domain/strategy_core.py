from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Literal

from btcbot.domain.models import normalize_symbol


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class OrderBookSummary:
    best_bid: Decimal
    best_ask: Decimal

    def __post_init__(self) -> None:
        if self.best_bid <= Decimal("0"):
            raise ValueError("best_bid must be > 0")
        if self.best_ask <= Decimal("0"):
            raise ValueError("best_ask must be > 0")
        if self.best_bid > self.best_ask:
            raise ValueError("best_bid must be <= best_ask")


@dataclass(frozen=True)
class PositionSummary:
    symbol: str
    qty: Decimal
    avg_cost: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))


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
    balances: Mapping[str, Decimal]
    position: PositionSummary | None
    open_orders: OpenOrdersSummary
    knobs: StrategyKnobs

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        normalized_balances = {
            str(asset).upper(): amount for asset, amount in dict(self.balances).items()
        }
        object.__setattr__(self, "balances", MappingProxyType(normalized_balances))


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: Literal["buy", "sell"]
    intent_type: Literal["place", "cancel", "replace"]
    target_notional_try: Decimal | None
    rationale: str
    strategy_id: str
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be one of: buy, sell")
        if self.intent_type not in {"place", "cancel", "replace"}:
            raise ValueError("intent_type must be one of: place, cancel, replace")

        if self.intent_type == "place":
            if self.target_notional_try is None:
                raise ValueError("target_notional_try is required when intent_type='place'")
            if self.target_notional_try <= Decimal("0"):
                raise ValueError("target_notional_try must be > 0 when intent_type='place'")
        elif self.target_notional_try is not None:
            raise ValueError("target_notional_try must be None when intent_type is cancel/replace")

        if self.confidence is None:
            return
        if self.confidence < Decimal("0") or self.confidence > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
