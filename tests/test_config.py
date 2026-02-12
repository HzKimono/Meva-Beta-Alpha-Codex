from __future__ import annotations

from pathlib import Path

import pytest

from btcbot.config import Settings


def test_parse_symbols_json_list() -> None:
    settings = Settings(SYMBOLS='["BTC_TRY","ETH_TRY","SOL_TRY"]')
    assert settings.symbols == ["BTC_TRY", "ETH_TRY", "SOL_TRY"]


def test_parse_symbols_csv() -> None:
    settings = Settings(SYMBOLS="BTC_TRY, ETH_TRY ,SOL_TRY")
    assert settings.symbols == ["BTC_TRY", "ETH_TRY", "SOL_TRY"]


def test_loads_values_from_env_file(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "KILL_SWITCH=false",
                "DRY_RUN=true",
                "TARGET_TRY=345",
                "SYMBOLS=BTC_TRY,ETH_TRY",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.kill_switch is False
    assert settings.dry_run is True
    assert settings.target_try == 345.0
    assert settings.symbols == ["BTC_TRY", "ETH_TRY"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("TARGET_TRY", -1),
        ("OFFSET_BPS", -1),
        ("TTL_SECONDS", 0),
        ("COOLDOWN_SECONDS", -1),
    ],
)
def test_invalid_settings_raise(field: str, value: int) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_parse_symbols_normalizes_case_and_quotes() -> None:
    settings = Settings(SYMBOLS='["btc_try", "Eth_Try", "SOL_TRY"]')
    assert settings.symbols == ["BTC_TRY", "ETH_TRY", "SOL_TRY"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MIN_ORDER_NOTIONAL_TRY", 0),
        ("DRY_RUN_TRY_BALANCE", -0.1),
    ],
)
def test_additional_invalid_settings_raise(field: str, value: float) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_btcturk_base_url_default() -> None:
    settings = Settings()
    assert settings.btcturk_base_url == "https://api.btcturk.com"


def test_live_trading_requires_ack() -> None:
    disabled = Settings(LIVE_TRADING=True, LIVE_TRADING_ACK="")
    enabled = Settings(LIVE_TRADING=True, LIVE_TRADING_ACK="I_UNDERSTAND")

    assert disabled.is_live_trading_enabled() is False
    assert enabled.is_live_trading_enabled() is True


def test_settings_defaults_are_prod_safe() -> None:
    settings = Settings()

    assert settings.kill_switch is True
    assert settings.dry_run is True
    assert settings.live_trading is False


def test_parse_symbols_csv_handles_whitespace_and_case() -> None:
    settings = Settings(SYMBOLS=" btc_try , Eth_Try,  sol_try ")

    assert settings.symbols == ["BTC_TRY", "ETH_TRY", "SOL_TRY"]
