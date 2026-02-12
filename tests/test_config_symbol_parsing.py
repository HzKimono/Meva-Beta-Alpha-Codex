from __future__ import annotations

import pytest

from btcbot.config import Settings


def test_symbols_csv_canonicalizes() -> None:
    settings = Settings(SYMBOLS="BTC_TRY,eth_try")
    assert settings.symbols == ["BTCTRY", "ETHTRY"]


def test_symbols_json_list_parses() -> None:
    settings = Settings(SYMBOLS='["BTC_TRY","ETH_TRY"]')
    assert settings.symbols == ["BTCTRY", "ETHTRY"]


def test_universe_allow_csv_canonicalizes() -> None:
    settings = Settings(UNIVERSE_ALLOW_SYMBOLS="btc_try")
    assert settings.universe_allow_symbols == ["BTCTRY"]


def test_empty_symbol_strings_return_empty_lists() -> None:
    settings = Settings(SYMBOLS="", UNIVERSE_ALLOW_SYMBOLS="", UNIVERSE_DENY_SYMBOLS="")
    assert settings.symbols == []
    assert settings.universe_allow_symbols == []
    assert settings.universe_deny_symbols == []


def test_symbols_invalid_json_object_has_symbols_message() -> None:
    with pytest.raises(ValueError, match="SYMBOLS JSON value must be a list"):
        Settings(SYMBOLS='{"BTC_TRY": true}')


def test_universe_invalid_json_object_has_universe_message() -> None:
    with pytest.raises(ValueError, match="UNIVERSE symbols JSON value must be a list"):
        Settings(UNIVERSE_ALLOW_SYMBOLS='{"BTC_TRY": true}')


def test_symbols_dedupes_preserving_first_seen_order() -> None:
    settings = Settings(SYMBOLS="BTC_TRY,btctry, ETH_TRY,ETH_TRY,BTC_TRY")
    assert settings.symbols == ["BTCTRY", "ETHTRY"]


def test_universe_allow_dedupes_preserving_first_seen_order() -> None:
    settings = Settings(UNIVERSE_ALLOW_SYMBOLS='["btc_try","BTCTRY","eth_try","ETHTRY"]')
    assert settings.universe_allow_symbols == ["BTCTRY", "ETHTRY"]
