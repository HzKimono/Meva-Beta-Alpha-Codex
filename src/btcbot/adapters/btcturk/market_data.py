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
    is_fresh: bool
    stale_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MarketDataBuildResult:
    snapshots: dict[str, MarketDataSnapshot]
    is_fresh: bool
    stale_reasons: tuple[str, ...]


class MarketDataSnapshotBuilder:
    def __init__(self) -> None:
        self._tops: dict[str, TopOfBook] = {}
        self._last_trade: dict[str, TradeTick] = {}

    def ingest_orderbook(self, *, symbol: str, bid: Decimal, ask: Decimal, ts_ms: int) -> None:
        self._tops[symbol] = TopOfBook(bid=bid, ask=ask, ts_ms=ts_ms)

    def ingest_trade(self, *, symbol: str, price: Decimal, qty: Decimal, ts_ms: int) -> None:
        self._last_trade[symbol] = TradeTick(price=price, qty=qty, ts_ms=ts_ms)

    def build(
        self,
        symbols: list[str],
        *,
        max_age_ms: int,
        now_ms: int | None = None,
    ) -> MarketDataBuildResult:
        resolved_now_ms = (
            now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
        )
        cycle_ts = datetime.fromtimestamp(resolved_now_ms / 1000, tz=UTC)

        stale_reasons_all: list[str] = []
        snapshots: dict[str, MarketDataSnapshot] = {}
        for symbol in symbols:
            symbol_stale_reasons: list[str] = []
            top = self._tops.get(symbol)
            last_trade = self._last_trade.get(symbol)

            if top is None:
                symbol_stale_reasons.append("missing_top")
            elif resolved_now_ms - top.ts_ms > max_age_ms:
                symbol_stale_reasons.append("stale_top")

            if last_trade is None:
                symbol_stale_reasons.append("missing_trade")
            elif resolved_now_ms - last_trade.ts_ms > max_age_ms:
                symbol_stale_reasons.append("stale_trade")

            if symbol_stale_reasons:
                stale_reasons_all.extend([f"{symbol}:{reason}" for reason in symbol_stale_reasons])

            snapshots[symbol] = MarketDataSnapshot(
                symbol=symbol,
                top=top,
                last_trade=last_trade,
                cycle_ts=cycle_ts,
                is_fresh=not symbol_stale_reasons,
                stale_reasons=tuple(symbol_stale_reasons),
            )

        return MarketDataBuildResult(
            snapshots=snapshots,
            is_fresh=not stale_reasons_all,
            stale_reasons=tuple(sorted(stale_reasons_all)),
        )


def should_observe_only(result: MarketDataBuildResult) -> bool:
    return not result.is_fresh
