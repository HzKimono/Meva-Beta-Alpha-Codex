from __future__ import annotations

from decimal import Decimal

from btcbot.domain.models import normalize_symbol
from btcbot.ports_price_conversion import FeeConversionRateError, PriceConverter


class MarkPriceConverter(PriceConverter):
    def __init__(self, mark_prices: dict[str, Decimal]) -> None:
        self._marks = {
            normalize_symbol(symbol): Decimal(str(price)) for symbol, price in mark_prices.items()
        }
        self._cache: dict[tuple[str, str], Decimal] = {}

    def __call__(self, base_currency: str, quote_currency: str) -> Decimal:
        base = base_currency.upper()
        quote = quote_currency.upper()
        if base == quote:
            return Decimal("1")

        key = (base, quote)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        direct_symbol = normalize_symbol(f"{base}{quote}")
        direct = self._marks.get(direct_symbol)
        if direct is not None and direct > 0:
            self._cache[key] = direct
            return direct

        inverse_symbol = normalize_symbol(f"{quote}{base}")
        inverse = self._marks.get(inverse_symbol)
        if inverse is not None and inverse > 0:
            rate = Decimal("1") / inverse
            self._cache[key] = rate
            return rate

        raise FeeConversionRateError(f"fee_conversion_missing_rate:{base}->{quote}")
