from __future__ import annotations

from decimal import Decimal

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


def test_universe_symbols_alias_parses_and_dedupes() -> None:
    settings = Settings(UNIVERSE_SYMBOLS=" btc_try, ETH_try ,btc_try , soltry ")
    assert settings.symbols == ["BTCTRY", "ETHTRY", "SOLTRY"]


def test_symbols_source_prefers_universe_symbols_env(monkeypatch) -> None:
    monkeypatch.setenv("UNIVERSE_SYMBOLS", "BTCTRY,ETHTRY")
    monkeypatch.setenv("SYMBOLS", "BTCTRY")
    settings = Settings()
    assert settings.symbols_source() == "env:UNIVERSE_SYMBOLS"


def test_symbols_source_legacy_alias_when_universe_symbols_missing(monkeypatch) -> None:
    monkeypatch.delenv("UNIVERSE_SYMBOLS", raising=False)
    monkeypatch.setenv("SYMBOLS", "BTCTRY")
    settings = Settings()
    assert settings.symbols_source() == "env:SYMBOLS"


def test_portfolio_targets_json_and_csv_parse_weights() -> None:
    settings_json = Settings(
        SYMBOLS="BTCTRY,ETHTRY,SOLTRY,AVAXTRY,ADATRY",
        PORTFOLIO_TARGETS='{"BTCTRY":0.4,"ETHTRY":0.2,"SOLTRY":0.2,"AVAXTRY":0.1,"ADATRY":0.1}',
    )
    assert settings_json.get_portfolio_target_weights(settings_json.symbols) == {
        "ADATRY": Decimal("0.1"),
        "AVAXTRY": Decimal("0.1"),
        "BTCTRY": Decimal("0.4"),
        "ETHTRY": Decimal("0.2"),
        "SOLTRY": Decimal("0.2"),
    }

    settings_csv = Settings(
        SYMBOLS="BTCTRY,ETHTRY,SOLTRY,AVAXTRY,ADATRY",
        PORTFOLIO_TARGETS="BTCTRY:0.4,ETHTRY:0.2,SOLTRY:0.2,AVAXTRY:0.1,ADATRY:0.1",
    )
    assert settings_csv.get_portfolio_target_weights(settings_csv.symbols) == {
        "ADATRY": Decimal("0.1"),
        "AVAXTRY": Decimal("0.1"),
        "BTCTRY": Decimal("0.4"),
        "ETHTRY": Decimal("0.2"),
        "SOLTRY": Decimal("0.2"),
    }


def test_portfolio_targets_invalid_inputs_raise_value_error() -> None:
    settings_negative = Settings(
        SYMBOLS="BTCTRY,ETHTRY",
        PORTFOLIO_TARGETS="BTCTRY:-0.2,ETHTRY:1.2",
    )
    with pytest.raises(ValueError, match="weight must be > 0"):
        settings_negative.get_portfolio_target_weights(settings_negative.symbols)

    settings_sum = Settings(
        SYMBOLS="BTCTRY,ETHTRY",
        PORTFOLIO_TARGETS="BTCTRY:0.5,ETHTRY:0.2",
    )
    with pytest.raises(ValueError, match="must sum to 1.0"):
        settings_sum.get_portfolio_target_weights(settings_sum.symbols)

    settings_unknown = Settings(
        SYMBOLS="BTCTRY,ETHTRY",
        PORTFOLIO_TARGETS="BTCTRY:0.5,SOLTRY:0.5",
    )
    with pytest.raises(ValueError, match="outside universe"):
        settings_unknown.get_portfolio_target_weights(settings_unknown.symbols)
