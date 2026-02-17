from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import SymbolRules, normalize_symbol, pair_info_to_symbol_rules


class SymbolRulesNotFoundError(ValueError):
    """Raised when symbol rules are unavailable for a requested symbol."""


@dataclass(frozen=True)
class MarketDataSnapshot:
    bids: dict[str, float]
    source: str
    fetched_at: datetime | None
    connected: bool
    missing_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketDataFreshness:
    is_stale: bool
    observed_age_ms: int | None
    max_age_ms: int
    source_mode: str
    connected: bool
    missing_symbols: tuple[str, ...]


class MarketDataProvider:
    def get_snapshot(self, symbols: list[str]) -> MarketDataSnapshot:
        raise NotImplementedError


class RestMarketDataProvider(MarketDataProvider):
    def __init__(
        self,
        *,
        exchange: ExchangeClient,
        now_provider: Callable[[], datetime],
    ) -> None:
        self.exchange = exchange
        self.now_provider = now_provider

    def get_snapshot(self, symbols: list[str]) -> MarketDataSnapshot:
        bids: dict[str, float] = {}
        for symbol in symbols:
            bid, _ask = self.exchange.get_orderbook(symbol)
            bids[symbol] = bid
        return MarketDataSnapshot(
            bids=bids,
            source="rest",
            fetched_at=self.now_provider(),
            connected=True,
        )


class WsMarketDataProvider(MarketDataProvider):
    def __init__(self) -> None:
        self._best_bids: dict[str, tuple[float, datetime]] = {}
        self._connected = False

    def set_connected(self, connected: bool) -> None:
        self._connected = connected

    def ingest_best_bid(self, symbol: str, bid: float, *, observed_at: datetime) -> None:
        self._best_bids[symbol] = (bid, observed_at)

    def get_snapshot(self, symbols: list[str]) -> MarketDataSnapshot:
        bids: dict[str, float] = {}
        seen_ts: list[datetime] = []
        missing_symbols: list[str] = []
        for symbol in symbols:
            cached = self._best_bids.get(symbol)
            if cached is None:
                missing_symbols.append(symbol)
                continue
            bid, ts = cached
            bids[symbol] = bid
            seen_ts.append(ts)
        return MarketDataSnapshot(
            bids=bids,
            source="ws",
            fetched_at=min(seen_ts) if seen_ts else None,
            connected=self._connected,
            missing_symbols=tuple(sorted(missing_symbols)),
        )


class MarketDataService:
    def __init__(
        self,
        exchange: ExchangeClient,
        *,
        rules_cache_ttl_seconds: int = 600,
        mode: str = "rest",
        ws_rest_fallback: bool = False,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.exchange = exchange
        self.rules_cache_ttl_seconds = rules_cache_ttl_seconds
        self._rules_cache: dict[str, SymbolRules] = {}
        self._rules_cache_loaded_at: datetime | None = None
        self.now_provider = now_provider or (lambda: datetime.now(UTC))
        self.mode = mode.strip().lower()
        self.ws_rest_fallback = ws_rest_fallback
        self._rest_provider = RestMarketDataProvider(exchange=exchange, now_provider=self.now_provider)
        self._ws_provider = WsMarketDataProvider()
        self._last_snapshot: MarketDataSnapshot | None = None

    def get_best_bid_ask(self, symbol: str) -> tuple[float, float]:
        return self.exchange.get_orderbook(symbol)

    def get_best_bids(self, symbols: list[str]) -> dict[str, float]:
        snapshot = self._resolve_provider_snapshot(symbols)
        self._last_snapshot = snapshot
        return snapshot.bids

    def _resolve_provider_snapshot(self, symbols: list[str]) -> MarketDataSnapshot:
        if self.mode == "ws":
            ws_snapshot = self._ws_provider.get_snapshot(symbols)
            if (
                self.ws_rest_fallback
                and (
                    not ws_snapshot.connected
                    or bool(ws_snapshot.missing_symbols)
                    or ws_snapshot.fetched_at is None
                )
            ):
                return self._rest_provider.get_snapshot(symbols)
            return ws_snapshot
        return self._rest_provider.get_snapshot(symbols)

    def set_ws_connected(self, connected: bool) -> None:
        self._ws_provider.set_connected(connected)

    def ingest_ws_best_bid(self, symbol: str, bid: float, *, observed_at: datetime | None = None) -> None:
        self._ws_provider.ingest_best_bid(
            symbol,
            bid,
            observed_at=observed_at or self.now_provider(),
        )

    def get_market_data_freshness(self, *, max_age_ms: int) -> MarketDataFreshness:
        snapshot = self._last_snapshot
        if snapshot is None:
            return MarketDataFreshness(
                is_stale=True,
                observed_age_ms=None,
                max_age_ms=max_age_ms,
                source_mode=self.mode,
                connected=False,
                missing_symbols=(),
            )

        observed_age_ms: int | None = None
        if snapshot.fetched_at is not None:
            observed_age_ms = int((self.now_provider() - snapshot.fetched_at).total_seconds() * 1000)

        is_stale = (
            snapshot.fetched_at is None
            or observed_age_ms is None
            or observed_age_ms > max_age_ms
            or not snapshot.connected
            or bool(snapshot.missing_symbols)
        )
        return MarketDataFreshness(
            is_stale=is_stale,
            observed_age_ms=observed_age_ms,
            max_age_ms=max_age_ms,
            source_mode=snapshot.source,
            connected=snapshot.connected,
            missing_symbols=snapshot.missing_symbols,
        )

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
