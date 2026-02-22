from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class FeeConversionRateError(RuntimeError):
    """Raised when a fee currency cannot be converted into the target currency."""


class PriceConverter(Protocol):
    def __call__(self, base_currency: str, quote_currency: str) -> Decimal: ...
