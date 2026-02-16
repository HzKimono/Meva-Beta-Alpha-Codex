from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import get_close_matches

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
    suggestions: dict[str, list[str]]
    auto_corrected_symbols: dict[str, str]


def resolve_effective_universe(settings: Settings) -> EffectiveUniverse:
    configured = [normalize_symbol(symbol) for symbol in settings.symbols]
    source = settings.symbols_source()

    client = BtcturkHttpClient(base_url=settings.btcturk_base_url, timeout=1.0)
    try:
        pairs = client.get_exchange_info()
        if not pairs:
            logger.warning("metadata unavailable; cannot validate symbols")
            return EffectiveUniverse(
                symbols=configured,
                rejected_symbols=[],
                metadata_available=False,
                source=source,
                suggestions={},
                auto_corrected_symbols={},
            )

        valid: set[str] = set()
        for pair in pairs:
            pair_symbol = getattr(pair, "pair_symbol", None)
            if pair_symbol is None and isinstance(pair, dict):
                pair_symbol = pair.get("pairSymbol") or pair.get("name")
            if pair_symbol is None:
                continue
            valid.add(normalize_symbol(str(pair_symbol)))

        rejected: list[str] = []
        effective: list[str] = []
        suggestions: dict[str, list[str]] = {}
        auto_corrected_symbols: dict[str, str] = {}
        for symbol in configured:
            if symbol in valid:
                effective.append(symbol)
                continue

            rejected.append(symbol)
            matches = [
                normalize_symbol(item)
                for item in get_close_matches(symbol, sorted(valid), n=3, cutoff=0.75)
            ]
            suggestions[symbol] = matches
            if settings.universe_auto_correct and len(matches) == 1:
                candidate = matches[0]
                if candidate in valid and candidate not in effective:
                    effective.append(candidate)
                    auto_corrected_symbols[symbol] = candidate

        return EffectiveUniverse(
            symbols=effective,
            rejected_symbols=rejected,
            metadata_available=True,
            source=source,
            suggestions=suggestions,
            auto_corrected_symbols=auto_corrected_symbols,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "universe_metadata_validation_skipped",
            extra={"extra": {"error_type": type(exc).__name__}},
        )
        logger.warning("metadata unavailable; cannot validate symbols")
        return EffectiveUniverse(
            symbols=configured,
            rejected_symbols=[],
            metadata_available=False,
            source=source,
            suggestions={},
            auto_corrected_symbols={},
        )
    finally:
        client.close()
