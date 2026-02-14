from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class OrderBookTop:
    ts: datetime
    best_bid: Decimal
    best_ask: Decimal


@dataclass(frozen=True)
class TickerStat:
    ts: datetime
    last: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal
    quote_volume: Decimal | None = None
