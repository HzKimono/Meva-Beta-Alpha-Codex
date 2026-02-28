from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from btcbot.observability import get_instrumentation

logger = logging.getLogger(__name__)


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
    decision: MarketDataDecision
    tradable_symbols: tuple[str, ...]


@dataclass(frozen=True)
class MarketDataDecision:
    decision: Literal["NORMAL", "DEGRADE", "OBSERVE_ONLY"]
    stale_symbols: frozenset[str]
    missing_trade_symbols: frozenset[str]
    missing_top_symbols: frozenset[str]
    stale_ratio: float


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
        mode: str = "LIVE",
        connected: bool = True,
        stale_ratio_threshold: float = 0.50,
        require_last_trade: bool = False,
    ) -> MarketDataBuildResult:
        resolved_now_ms = (
            now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
        )
        cycle_ts = datetime.fromtimestamp(resolved_now_ms / 1000, tz=UTC)
        normalized_mode = mode.strip().upper()

        stale_reasons_all: list[str] = []
        snapshots: dict[str, MarketDataSnapshot] = {}
        stale_symbols_for_trading: set[str] = set()
        missing_trade_symbols: set[str] = set()
        missing_top_symbols: set[str] = set()
        reason_counter: Counter[str] = Counter()
        for symbol in symbols:
            symbol_stale_reasons: list[str] = []
            top = self._tops.get(symbol)
            last_trade = self._last_trade.get(symbol)
            stale_for_trading = False

            if top is None:
                symbol_stale_reasons.append("missing_top")
                missing_top_symbols.add(symbol)
                stale_for_trading = True
            elif resolved_now_ms - top.ts_ms > max_age_ms:
                symbol_stale_reasons.append("stale_top")
                stale_for_trading = True

            if last_trade is None:
                symbol_stale_reasons.append("missing_trade")
                missing_trade_symbols.add(symbol)
                if require_last_trade:
                    stale_for_trading = True
            elif resolved_now_ms - last_trade.ts_ms > max_age_ms:
                symbol_stale_reasons.append("stale_trade")
                if require_last_trade:
                    stale_for_trading = True

            if stale_for_trading:
                stale_symbols_for_trading.add(symbol)

            if symbol_stale_reasons:
                stale_reasons_all.extend([f"{symbol}:{reason}" for reason in symbol_stale_reasons])
                reason_counter.update(symbol_stale_reasons)

            snapshots[symbol] = MarketDataSnapshot(
                symbol=symbol,
                top=top,
                last_trade=last_trade,
                cycle_ts=cycle_ts,
                is_fresh=not stale_for_trading,
                stale_reasons=tuple(symbol_stale_reasons),
            )

        symbols_total = len(symbols)
        stale_ratio = (len(stale_symbols_for_trading) / symbols_total) if symbols_total else 1.0
        decision = "NORMAL"

        if not connected or symbols_total == 0:
            decision = "OBSERVE_ONLY"
        elif len(stale_symbols_for_trading) == symbols_total:
            decision = "OBSERVE_ONLY"
        elif stale_ratio > stale_ratio_threshold:
            decision = "OBSERVE_ONLY"
        elif missing_trade_symbols:
            decision = "DEGRADE"

        market_data_decision = MarketDataDecision(
            decision=decision,
            stale_symbols=frozenset(stale_symbols_for_trading),
            missing_trade_symbols=frozenset(missing_trade_symbols),
            missing_top_symbols=frozenset(missing_top_symbols),
            stale_ratio=stale_ratio,
        )

        stale_symbols_sample = sorted(stale_symbols_for_trading)[:10]
        log_payload = {
            "event": "market_data_freshness_decision",
            "mode": normalized_mode,
            "connected": connected,
            "symbols_total": symbols_total,
            "symbols_stale_count": len(stale_symbols_for_trading),
            "symbols_missing_top_count": len(missing_top_symbols),
            "symbols_missing_trade_count": len(missing_trade_symbols),
            "stale_ratio": stale_ratio,
            "threshold": stale_ratio_threshold,
            "decision": decision,
            "stale_symbols_sample": stale_symbols_sample,
            "reasons_summary": dict(reason_counter),
        }
        log_fn = logger.warning if decision != "NORMAL" else logger.info
        log_fn("market_data_freshness_decision", extra={"extra": log_payload})

        instrumentation = get_instrumentation()
        if missing_trade_symbols:
            instrumentation.counter(
                "bot_market_data_missing_trade_total", len(missing_trade_symbols)
            )
        if stale_symbols_for_trading:
            instrumentation.counter(
                "bot_market_data_stale_symbols_total", len(stale_symbols_for_trading)
            )
        if decision == "OBSERVE_ONLY":
            instrumentation.counter("bot_market_data_observe_only_due_to_market_data_total", 1)

        tradable_symbols = tuple(
            sorted(symbol for symbol in symbols if symbol not in stale_symbols_for_trading)
        )

        return MarketDataBuildResult(
            snapshots=snapshots,
            is_fresh=decision != "OBSERVE_ONLY",
            stale_reasons=tuple(sorted(stale_reasons_all)),
            decision=market_data_decision,
            tradable_symbols=tradable_symbols,
        )


def should_observe_only(result: MarketDataBuildResult) -> bool:
    return result.decision.decision == "OBSERVE_ONLY"
