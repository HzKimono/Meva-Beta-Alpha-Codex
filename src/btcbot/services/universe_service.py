from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.strategy_core import OrderBookSummary
from btcbot.domain.symbols import canonical_symbol, quote_currency
from btcbot.domain.universe_models import SymbolInfo, UniverseKnobs


@dataclass(frozen=True)
class _RankedSymbol:
    symbol: str
    spread_bps: Decimal | None
    volume_try: Decimal | None


def _normalize_symbols(symbols: tuple[str, ...]) -> set[str]:
    return {canonical_symbol(value) for value in symbols}


def _spread_bps(orderbook: OrderBookSummary) -> Decimal | None:
    mid = (orderbook.best_bid + orderbook.best_ask) / Decimal("2")
    if mid <= Decimal("0"):
        return None
    return ((orderbook.best_ask - orderbook.best_bid) / mid) * Decimal("10000")


def _quote_matches(symbol: SymbolInfo, expected_quote: str) -> bool:
    if symbol.quote:
        return symbol.quote == expected_quote

    try:
        return quote_currency(symbol.symbol) == expected_quote
    except ValueError:
        return False


def select_universe(
    *,
    symbols: list[SymbolInfo],
    orderbooks: dict[str, OrderBookSummary] | None,
    knobs: UniverseKnobs,
) -> list[str]:
    """Select and rank a deterministic trading universe offline for future Stage 5 integration."""

    expected_quote = knobs.quote_currency.upper()
    allow_set = _normalize_symbols(knobs.allow_symbols)
    deny_set = _normalize_symbols(knobs.deny_symbols)
    normalized_orderbooks = {
        canonical_symbol(symbol): value for symbol, value in (orderbooks or {}).items()
    }

    ranked: list[_RankedSymbol] = []
    for item in symbols:
        symbol = SymbolInfo(
            symbol=item.symbol,
            base=item.base,
            quote=item.quote,
            active=item.active,
            min_notional_try=item.min_notional_try,
            volume_try=item.volume_try,
        )

        if allow_set and symbol.symbol not in allow_set:
            continue
        if symbol.symbol in deny_set:
            continue
        if knobs.require_try_quote and not _quote_matches(symbol, expected_quote):
            continue
        if knobs.require_active and not symbol.active:
            continue

        orderbook = normalized_orderbooks.get(symbol.symbol)
        spread_bps = None
        if orderbook is not None:
            spread_bps = _spread_bps(orderbook)
            if spread_bps is None:
                continue
            if spread_bps > knobs.max_spread_bps:
                continue

            mid = (orderbook.best_bid + orderbook.best_ask) / Decimal("2")
            effective_min_notional = max(
                knobs.min_notional_try,
                symbol.min_notional_try if symbol.min_notional_try is not None else Decimal("0"),
            )
            if mid < effective_min_notional:
                continue

        ranked.append(
            _RankedSymbol(symbol=symbol.symbol, spread_bps=spread_bps, volume_try=symbol.volume_try)
        )

    def _sort_key(item: _RankedSymbol) -> tuple[Decimal, Decimal, str]:
        spread_key = item.spread_bps if item.spread_bps is not None else Decimal("Infinity")
        volume_key = item.volume_try if item.volume_try is not None else Decimal("0")
        return (spread_key, -volume_key, item.symbol)

    ranked.sort(key=_sort_key)
    max_size = max(0, knobs.max_universe_size)
    return [item.symbol for item in ranked[:max_size]]
