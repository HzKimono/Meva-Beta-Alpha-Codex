from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True)
class TopOfBook:
    bid: Decimal
    ask: Decimal
    ts_ms: int


@dataclass(frozen=True)
class TradeTick:
    price: Decimal
    qty: Decimal
    ts_ms: int


@dataclass(frozen=True)
class MarketDataSnapshot:
    symbol: str
    top: TopOfBook | None
    last_trade: TradeTick | None
    cycle_ts: datetime


class MarketDataSnapshotBuilder:
    def __init__(self) -> None:
        self._tops: dict[str, TopOfBook] = {}
        self._last_trade: dict[str, TradeTick] = {}

    def ingest_orderbook(self, *, symbol: str, bid: Decimal, ask: Decimal, ts_ms: int) -> None:
        self._tops[symbol] = TopOfBook(bid=bid, ask=ask, ts_ms=ts_ms)

    def ingest_trade(self, *, symbol: str, price: Decimal, qty: Decimal, ts_ms: int) -> None:
        self._last_trade[symbol] = TradeTick(price=price, qty=qty, ts_ms=ts_ms)

    def build(self, symbols: list[str]) -> dict[str, MarketDataSnapshot]:
        cycle_ts = datetime.now(UTC)
        return {
            symbol: MarketDataSnapshot(
                symbol=symbol,
                top=self._tops.get(symbol),
                last_trade=self._last_trade.get(symbol),
                cycle_ts=cycle_ts,
            )
            for symbol in symbols
        }
