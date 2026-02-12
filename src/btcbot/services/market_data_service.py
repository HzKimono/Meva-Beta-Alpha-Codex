from __future__ import annotations

from datetime import UTC, datetime, timedelta

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import SymbolRules, normalize_symbol, pair_info_to_symbol_rules


class SymbolRulesNotFoundError(ValueError):
    """Raised when symbol rules are unavailable for a requested symbol."""


class MarketDataService:
    def __init__(self, exchange: ExchangeClient, *, rules_cache_ttl_seconds: int = 600):
        self.exchange = exchange
        self.rules_cache_ttl_seconds = rules_cache_ttl_seconds
        self._rules_cache: dict[str, SymbolRules] = {}
        self._rules_cache_loaded_at: datetime | None = None

    def get_best_bid_ask(self, symbol: str) -> tuple[float, float]:
        return self.exchange.get_orderbook(symbol)

    def get_best_bids(self, symbols: list[str]) -> dict[str, float]:
        bids: dict[str, float] = {}
        for symbol in symbols:
            bid, _ask = self.get_best_bid_ask(symbol)
            bids[symbol] = bid
        return bids

    def _cache_expired(self, now: datetime) -> bool:
        if self._rules_cache_loaded_at is None:
            return True
        return now - self._rules_cache_loaded_at >= timedelta(seconds=self.rules_cache_ttl_seconds)

    def _refresh_symbol_rules_cache(self) -> None:
        pairs = self.exchange.get_exchange_info()
        cache: dict[str, SymbolRules] = {}
        for pair in pairs:
            rules = pair_info_to_symbol_rules(pair)
            cache[normalize_symbol(pair.pair_symbol)] = rules
        self._rules_cache = cache
        self._rules_cache_loaded_at = datetime.now(UTC)

    def get_symbol_rules(self, pair_symbol: str) -> SymbolRules:
        now = datetime.now(UTC)
        if self._cache_expired(now):
            self._refresh_symbol_rules_cache()

        requested_symbol = pair_symbol
        canonical_symbol = normalize_symbol(pair_symbol)
        rules = self._rules_cache.get(canonical_symbol)
        if rules is None:
            raise SymbolRulesNotFoundError(
                "Unknown symbol rules for pair "
                f"requested={requested_symbol} canonical={canonical_symbol}; "
                "exchangeinfo unavailable or symbol missing from rules cache"
            )
        return rules
