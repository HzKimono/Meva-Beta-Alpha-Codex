from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from typing import TYPE_CHECKING

from btcbot.domain.symbols import canonical_symbol, quote_currency
from btcbot.domain.universe import UniverseCandidate, UniverseSelectionResult

if TYPE_CHECKING:
    from btcbot.config import Settings


_BPS = Decimal("10000")
_MISSING_METRIC_PENALTY = Decimal("-1")
_DEFAULT_WEIGHT_LIQUIDITY = Decimal("0.50")
_DEFAULT_WEIGHT_SPREAD = Decimal("0.30")
_DEFAULT_WEIGHT_VOLATILITY = Decimal("0.20")


@dataclass(frozen=True)
class _RawMetrics:
    volume_try: Decimal | None
    spread_bps: Decimal | None
    volatility: Decimal | None


class UniverseSelectionService:
    def select_universe(
        self, *, exchange: object, settings: Settings, now_utc: datetime
    ) -> UniverseSelectionResult:
        exchange_info = self._safe_get_exchange_info(exchange)
        symbols = self._filter_symbols(exchange_info=exchange_info, settings=settings)
        ticker_stats = self._fetch_ticker_stats(exchange)
        candles_cache: dict[str, list[Decimal]] = {}
        orderbook_cache: dict[str, tuple[Decimal, Decimal] | None] = {}

        raw_metrics: dict[str, _RawMetrics] = {}
        for symbol in symbols:
            volume_try = self._extract_quote_volume_try(symbol=symbol, ticker_stats=ticker_stats)
            spread_bps = self._fetch_spread_bps(
                exchange=exchange, symbol=symbol, cache=orderbook_cache
            )
            volatility = self._fetch_volatility(
                exchange=exchange,
                symbol=symbol,
                settings=settings,
                ticker_stats=ticker_stats,
                candles_cache=candles_cache,
            )
            raw_metrics[symbol] = _RawMetrics(
                volume_try=volume_try,
                spread_bps=spread_bps,
                volatility=volatility,
            )

        candidates = self._score_candidates(raw_metrics=raw_metrics, settings=settings)
        capped = candidates[: max(0, settings.stage7_universe_size)]
        reasons = [
            "deterministic_ranking: total desc -> liquidity desc -> symbol asc",
            "missing_metrics_penalty: -1 applied per missing score component",
            "dry_run_read_only: universe selection never places orders",
        ]
        return UniverseSelectionResult(
            selected_symbols=[item.symbol for item in capped],
            scored=candidates,
            reasons=reasons,
            timestamp=now_utc,
        )

    def _safe_get_exchange_info(self, exchange: object) -> list[object]:
        getter = getattr(exchange, "get_exchange_info", None)
        if not callable(getter):
            return []
        try:
            rows = getter()
        except Exception:  # noqa: BLE001
            return []
        return list(rows)

    def _filter_symbols(self, *, exchange_info: list[object], settings: Settings) -> list[str]:
        allow = set(settings.stage7_universe_whitelist)
        deny = set(settings.stage7_universe_blacklist)
        out: list[str] = []
        for pair in exchange_info:
            raw_symbol = getattr(pair, "pair_symbol", None) or getattr(pair, "pairSymbol", None)
            if not raw_symbol:
                continue
            symbol = canonical_symbol(str(raw_symbol))
            if allow and symbol not in allow:
                continue
            if symbol in deny:
                continue
            if (
                settings.stage7_universe_quote_ccy
                and quote_currency(symbol) != settings.stage7_universe_quote_ccy
            ):
                continue
            out.append(symbol)
        if allow:
            for symbol in sorted(allow):
                if symbol in deny:
                    continue
                if (
                    settings.stage7_universe_quote_ccy
                    and quote_currency(symbol) != settings.stage7_universe_quote_ccy
                ):
                    continue
                if symbol not in out:
                    out.append(symbol)
        return sorted(set(out))

    def _fetch_ticker_stats(self, exchange: object) -> dict[str, dict[str, Decimal]]:
        getter = getattr(exchange, "get_ticker_stats", None)
        if not callable(getter):
            return {}
        try:
            rows = getter()
        except Exception:  # noqa: BLE001
            return {}
        parsed: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol_raw = row.get("pairSymbol") or row.get("pair") or row.get("symbol")
            if symbol_raw is None:
                continue
            symbol = canonical_symbol(str(symbol_raw))
            parsed[symbol] = {
                "volume": self._as_decimal(row.get("volume") or row.get("quoteVolume")),
                "last": self._as_decimal(
                    row.get("last") or row.get("lastPrice") or row.get("close")
                ),
                "high": self._as_decimal(row.get("high") or row.get("highPrice")),
                "low": self._as_decimal(row.get("low") or row.get("lowPrice")),
                "price_change": self._as_decimal(
                    row.get("priceChangePercent") or row.get("dailyPercent")
                ),
            }
        return parsed

    def _extract_quote_volume_try(
        self, *, symbol: str, ticker_stats: dict[str, dict[str, Decimal]]
    ) -> Decimal | None:
        stats = ticker_stats.get(symbol)
        if stats is None:
            return None
        return stats.get("volume")

    def _fetch_spread_bps(
        self,
        *,
        exchange: object,
        symbol: str,
        cache: dict[str, tuple[Decimal, Decimal] | None],
    ) -> Decimal | None:
        if symbol not in cache:
            getter = getattr(exchange, "get_orderbook", None)
            if not callable(getter):
                cache[symbol] = None
            else:
                try:
                    bid, ask = getter(symbol)
                    cache[symbol] = (Decimal(str(bid)), Decimal(str(ask)))
                except Exception:  # noqa: BLE001
                    cache[symbol] = None
        raw = cache[symbol]
        if raw is None:
            return None
        bid, ask = raw
        return self.compute_spread_bps(best_bid=bid, best_ask=ask)

    @staticmethod
    def compute_spread_bps(*, best_bid: Decimal, best_ask: Decimal) -> Decimal | None:
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None
        mid = (best_bid + best_ask) / Decimal("2")
        if mid <= 0:
            return None
        return ((best_ask - best_bid) / mid) * _BPS

    def _fetch_volatility(
        self,
        *,
        exchange: object,
        symbol: str,
        settings: Settings,
        ticker_stats: dict[str, dict[str, Decimal]],
        candles_cache: dict[str, list[Decimal]],
    ) -> Decimal | None:
        closes = self._fetch_candle_closes(
            exchange=exchange,
            symbol=symbol,
            lookback=max(2, settings.stage7_vol_lookback),
            cache=candles_cache,
        )
        if len(closes) >= 2:
            return self._compute_return_std(closes)

        stats = ticker_stats.get(symbol)
        if stats is None:
            return None
        last = stats.get("last")
        high = stats.get("high")
        low = stats.get("low")
        if last is not None and high is not None and low is not None and last > 0 and high >= low:
            return (high - low) / last
        change = stats.get("price_change")
        if change is None:
            return None
        return change.copy_abs() / Decimal("100")

    def _fetch_candle_closes(
        self,
        *,
        exchange: object,
        symbol: str,
        lookback: int,
        cache: dict[str, list[Decimal]],
    ) -> list[Decimal]:
        if symbol in cache:
            return cache[symbol]
        getter = getattr(exchange, "get_candles", None)
        if not callable(getter):
            cache[symbol] = []
            return []
        try:
            rows = getter(symbol, lookback)
        except Exception:  # noqa: BLE001
            cache[symbol] = []
            return []

        closes: list[Decimal] = []
        for row in rows:
            if isinstance(row, dict):
                close_val = self._as_decimal(row.get("close") or row.get("c"))
            else:
                close_val = self._as_decimal(getattr(row, "close", None))
            if close_val is None or close_val <= 0:
                continue
            closes.append(close_val)
        cache[symbol] = closes
        return closes

    @staticmethod
    def _compute_return_std(closes: Sequence[Decimal]) -> Decimal:
        returns: list[float] = []
        for prev, curr in zip(closes[:-1], closes[1:], strict=False):
            if prev <= 0:
                continue
            returns.append(float((curr - prev) / prev))
        if not returns:
            return Decimal("0")
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / len(returns)
        return Decimal(str(sqrt(variance)))

    @staticmethod
    def _as_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None

    def _score_candidates(
        self,
        *,
        raw_metrics: dict[str, _RawMetrics],
        settings: Settings,
    ) -> list[UniverseCandidate]:
        weights = self._resolve_weights(settings)
        liquidity_values = [
            item.volume_try for item in raw_metrics.values() if item.volume_try is not None
        ]
        spread_values = [
            item.spread_bps for item in raw_metrics.values() if item.spread_bps is not None
        ]
        volatility_values = [
            item.volatility for item in raw_metrics.values() if item.volatility is not None
        ]

        liquidity_min, liquidity_max = self._bounds(liquidity_values)
        spread_min, spread_max = self._bounds(spread_values)
        volatility_min, volatility_max = self._bounds(volatility_values)

        scored: list[UniverseCandidate] = []
        for symbol, metrics in raw_metrics.items():
            liquidity_score = self._normalize_linear(
                metrics.volume_try, liquidity_min, liquidity_max
            )
            spread_score = self._normalize_inverse(metrics.spread_bps, spread_min, spread_max)
            volatility_score = self._normalize_inverse(
                metrics.volatility,
                volatility_min,
                volatility_max,
            )

            total_score = (
                liquidity_score * weights["liquidity"]
                + spread_score * weights["spread"]
                + volatility_score * weights["volatility"]
            )

            if (
                metrics.volume_try is not None
                and metrics.volume_try < settings.stage7_min_quote_volume_try
            ):
                total_score -= Decimal("5")
            if (
                metrics.spread_bps is not None
                and settings.stage7_max_spread_bps > 0
                and metrics.spread_bps > settings.stage7_max_spread_bps
            ):
                total_score -= Decimal("5")

            scored.append(
                UniverseCandidate(
                    symbol=symbol,
                    liquidity_score=liquidity_score,
                    spread_score=spread_score,
                    volatility_score=volatility_score,
                    total_score=total_score,
                    breakdown={
                        "liquidity_score": str(liquidity_score),
                        "spread_score": str(spread_score),
                        "volatility_score": str(volatility_score),
                        "total_score": str(total_score),
                        "volume_try": str(metrics.volume_try)
                        if metrics.volume_try is not None
                        else "missing",
                        "spread_bps": str(metrics.spread_bps)
                        if metrics.spread_bps is not None
                        else "missing",
                        "volatility": str(metrics.volatility)
                        if metrics.volatility is not None
                        else "missing",
                    },
                )
            )

        scored.sort(
            key=lambda item: (
                -item.total_score,
                -item.liquidity_score,
                item.symbol,
            )
        )
        return scored

    @staticmethod
    def _resolve_weights(settings: Settings) -> dict[str, Decimal]:
        configured = settings.stage7_score_weights or {}
        liquidity = Decimal(str(configured.get("liquidity", _DEFAULT_WEIGHT_LIQUIDITY)))
        spread = Decimal(str(configured.get("spread", _DEFAULT_WEIGHT_SPREAD)))
        volatility = Decimal(str(configured.get("volatility", _DEFAULT_WEIGHT_VOLATILITY)))
        total = liquidity + spread + volatility
        if total <= 0:
            return {
                "liquidity": _DEFAULT_WEIGHT_LIQUIDITY,
                "spread": _DEFAULT_WEIGHT_SPREAD,
                "volatility": _DEFAULT_WEIGHT_VOLATILITY,
            }
        return {
            "liquidity": liquidity / total,
            "spread": spread / total,
            "volatility": volatility / total,
        }

    @staticmethod
    def _bounds(values: list[Decimal]) -> tuple[Decimal, Decimal]:
        if not values:
            return Decimal("0"), Decimal("0")
        return min(values), max(values)

    @staticmethod
    def _normalize_linear(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal:
        if value is None:
            return _MISSING_METRIC_PENALTY
        if high <= low:
            return Decimal("1")
        return (value - low) / (high - low)

    @staticmethod
    def _normalize_inverse(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal:
        if value is None:
            return _MISSING_METRIC_PENALTY
        if high <= low:
            return Decimal("1")
        return Decimal("1") - ((value - low) / (high - low))
