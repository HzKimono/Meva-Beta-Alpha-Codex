from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.accounting import Position
from btcbot.domain.models import Balance


@dataclass(frozen=True)
class StrategyContext:
    cycle_id: str
    symbols: list[str]
    orderbooks: dict[str, tuple[Decimal, Decimal]]
    positions: dict[str, Position]
    balances: list[Balance]
    open_orders: dict[str, int]
    settings: Settings
