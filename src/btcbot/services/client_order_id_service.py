from __future__ import annotations

import hashlib

from btcbot.domain.symbols import canonical_symbol

_BTCTURK_CLIENT_ID_MAX_LEN = 50


def build_exchange_client_id(*, internal_client_id: str, symbol: str, side: str) -> str:
    """Build deterministic BTCTurk-safe client id from a stable internal id."""

    normalized_symbol = canonical_symbol(symbol)
    side_token = side.strip().lower()[:1] or "x"
    symbol_token = normalized_symbol.replace("_", "")[:6].lower()
    digest = hashlib.sha256(internal_client_id.encode("utf-8")).hexdigest()[:32]
    exchange_client_id = f"b4-{symbol_token}-{side_token}-{digest}"
    return exchange_client_id[:_BTCTURK_CLIENT_ID_MAX_LEN]


def is_btcturk_client_id_safe(client_id: str) -> bool:
    return 0 < len(client_id) <= _BTCTURK_CLIENT_ID_MAX_LEN
