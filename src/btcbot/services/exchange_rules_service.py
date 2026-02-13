from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import pair_info_to_symbol_rules
from btcbot.domain.stage4 import ExchangeRules, Quantizer

logger = logging.getLogger(__name__)


def _norm_symbol(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def _pair_symbol_candidates(pair: object) -> list[str]:
    candidates = [
        getattr(pair, "pair_symbol", None),
        getattr(pair, "symbol", None),
        getattr(pair, "name", None),
        getattr(pair, "name_normalized", None),
        getattr(pair, "nameNormalized", None),
    ]
    return [candidate for candidate in candidates if isinstance(candidate, str) and candidate]


@dataclass
class _CachedRules:
    rules: ExchangeRules
    expires_at: datetime


class ExchangeRulesService:
    def __init__(self, exchange: ExchangeClient, *, cache_ttl_sec: int = 300) -> None:
        self.exchange = exchange
        self.cache_ttl_sec = max(1, cache_ttl_sec)
        self._cache: dict[str, _CachedRules] = {}

    def get_rules(self, symbol: str) -> ExchangeRules:
        key = _norm_symbol(symbol)
        now = datetime.now(UTC)
        cached = self._cache.get(key)
        if cached and cached.expires_at > now:
            return cached.rules

        pairs = self.exchange.get_exchange_info()
        index: dict[str, object] = {}
        for pair in pairs:
            for candidate in _pair_symbol_candidates(pair):
                index[_norm_symbol(candidate)] = pair

        match = index.get(key)
        if match is None:
            sample_symbols = sorted(index.keys())[:10]
            logger.debug(
                "exchange_rules_lookup_miss symbol=%s normalized=%s sample_available=%s",
                symbol,
                key,
                sample_symbols,
            )
            raise ValueError(f"No exchange rules for symbol={symbol} normalized={key}")

        rules = pair_info_to_symbol_rules(match)
        stage4_rules = ExchangeRules(
            tick_size=rules.tick_size or Decimal("0"),
            step_size=rules.step_size or Decimal("0"),
            min_notional_try=rules.min_total or Decimal("0"),
            price_precision=rules.price_scale,
            qty_precision=rules.quantity_scale,
        )
        cached_rules = _CachedRules(
            rules=stage4_rules,
            expires_at=now + timedelta(seconds=self.cache_ttl_sec),
        )
        for alias in _pair_symbol_candidates(match):
            self._cache[_norm_symbol(alias)] = cached_rules
        self._cache[key] = cached_rules
        return stage4_rules

    def quantize_price(self, symbol: str, price: Decimal) -> Decimal:
        return Quantizer.quantize_price(price, self.get_rules(symbol))

    def quantize_qty(self, symbol: str, qty: Decimal) -> Decimal:
        return Quantizer.quantize_qty(qty, self.get_rules(symbol))

    def validate_min_notional(self, symbol: str, price: Decimal, qty: Decimal) -> bool:
        return Quantizer.validate_min_notional(price, qty, self.get_rules(symbol))
