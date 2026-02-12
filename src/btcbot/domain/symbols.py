from __future__ import annotations


def canonical_symbol(symbol: str) -> str:
    """Return canonical exchange symbol representation (e.g. BTCTRY)."""

    return symbol.replace("_", "").upper()


def split_symbol(symbol: str) -> tuple[str, str]:
    """Split a canonicalized symbol into (base, quote).

    Supports underscore form (e.g. BTC_TRY) and canonical concatenated form
    (e.g. BTCTRY). For concatenated symbols, quote is inferred from known
    suffixes.
    """

    normalized = canonical_symbol(symbol)
    if "_" in symbol:
        base_raw, quote_raw = symbol.split("_", 1)
        return base_raw.upper(), quote_raw.upper()

    known_quotes = ("TRY", "USDT", "USDC", "BTC", "ETH", "EUR", "USD")
    for quote in known_quotes:
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)], quote

    raise ValueError(f"Could not split symbol into base/quote: {symbol}")


def quote_currency(symbol: str) -> str:
    """Return quote currency from a symbol (e.g. TRY for BTC_TRY/BTCTRY)."""

    _base, quote = split_symbol(symbol)
    return quote
