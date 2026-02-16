from __future__ import annotations

import logging
from dataclasses import dataclass

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.config import Settings
from btcbot.domain.models import normalize_symbol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffectiveUniverse:
    symbols: list[str]
    rejected_symbols: list[str]
    metadata_available: bool
    source: str


def resolve_effective_universe(settings: Settings) -> EffectiveUniverse:
    configured = [normalize_symbol(symbol) for symbol in settings.symbols]
    source = settings.symbols_source()

    client = BtcturkHttpClient(base_url=settings.btcturk_base_url, timeout=1.0)
    try:
        pairs = client.get_exchange_info()
        if not pairs:
            return EffectiveUniverse(
                symbols=configured,
                rejected_symbols=[],
                metadata_available=False,
                source=source,
            )

        valid: set[str] = set()
        for pair in pairs:
            pair_symbol = getattr(pair, "pair_symbol", None)
            if pair_symbol is None and isinstance(pair, dict):
                pair_symbol = pair.get("pairSymbol") or pair.get("name")
            if pair_symbol is None:
                continue
            valid.add(normalize_symbol(str(pair_symbol)))

        rejected = [symbol for symbol in configured if symbol not in valid]
        effective = [symbol for symbol in configured if symbol in valid]
        return EffectiveUniverse(
            symbols=effective,
            rejected_symbols=rejected,
            metadata_available=True,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "universe_metadata_validation_skipped",
            extra={"extra": {"error_type": type(exc).__name__}},
        )
        return EffectiveUniverse(
            symbols=configured,
            rejected_symbols=[],
            metadata_available=False,
            source=source,
        )
    finally:
        client.close()
