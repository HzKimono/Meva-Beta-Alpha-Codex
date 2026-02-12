from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import pair_info_to_symbol_rules
from btcbot.domain.stage4 import ExchangeRules, Quantizer


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
        now = datetime.now(UTC)
        cached = self._cache.get(symbol)
        if cached and cached.expires_at > now:
            return cached.rules

        pairs = self.exchange.get_exchange_info()
        for pair in pairs:
            if pair.pair_symbol == symbol:
                rules = pair_info_to_symbol_rules(pair)
                stage4_rules = ExchangeRules(
                    tick_size=rules.tick_size or Decimal("0"),
                    step_size=rules.step_size or Decimal("0"),
                    min_notional_try=rules.min_total or Decimal("0"),
                    price_precision=rules.price_scale,
                    qty_precision=rules.quantity_scale,
                )
                self._cache[symbol] = _CachedRules(
                    rules=stage4_rules,
                    expires_at=now + timedelta(seconds=self.cache_ttl_sec),
                )
                return stage4_rules
        raise ValueError(f"No exchange rules for symbol={symbol}")

    def quantize_price(self, symbol: str, price: Decimal) -> Decimal:
        return Quantizer.quantize_price(price, self.get_rules(symbol))

    def quantize_qty(self, symbol: str, qty: Decimal) -> Decimal:
        return Quantizer.quantize_qty(qty, self.get_rules(symbol))

    def validate_min_notional(self, symbol: str, price: Decimal, qty: Decimal) -> bool:
        return Quantizer.validate_min_notional(price, qty, self.get_rules(symbol))
