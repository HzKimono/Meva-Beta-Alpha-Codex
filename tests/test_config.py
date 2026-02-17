from __future__ import annotations

from pathlib import Path

import pytest

from btcbot.config import Settings
from btcbot.domain.anomalies import AnomalyCode


def test_parse_symbols_json_list() -> None:
    settings = Settings(SYMBOLS='["BTC_TRY","ETH_TRY","SOL_TRY"]')
    assert settings.symbols == ["BTCTRY", "ETHTRY", "SOLTRY"]


def test_parse_symbols_csv() -> None:
    settings = Settings(SYMBOLS="BTC_TRY, ETH_TRY ,SOL_TRY")
    assert settings.symbols == ["BTCTRY", "ETHTRY", "SOLTRY"]


def test_loads_values_from_env_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("UNIVERSE_SYMBOLS", raising=False)
    monkeypatch.delenv("SYMBOLS", raising=False)
    monkeypatch.delenv("UNIVERSE_AUTO_CORRECT", raising=False)

    env_file = tmp_path / ".env.live"
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

    settings = Settings(_env_file=str(env_file))

    assert settings.kill_switch is False
    assert settings.dry_run is True
    assert settings.target_try == 345.0
    assert settings.symbols == ["BTCTRY", "ETHTRY"]


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
    assert settings.symbols == ["BTCTRY", "ETHTRY", "SOLTRY"]


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
    with pytest.raises(ValueError):
        Settings(LIVE_TRADING=True, LIVE_TRADING_ACK="", KILL_SWITCH=False)

    enabled = Settings(
        LIVE_TRADING=True,
        SAFE_MODE=False,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        KILL_SWITCH=False,
        BTCTURK_API_KEY="key",
        BTCTURK_API_SECRET="secret",
        DRY_RUN=False,
    )

    assert enabled.is_live_trading_enabled() is True


def test_live_trading_requires_keys_and_kill_switch_off() -> None:
    with pytest.raises(ValueError):
        Settings(LIVE_TRADING=True, LIVE_TRADING_ACK="I_UNDERSTAND", DRY_RUN=False)

    with pytest.raises(ValueError):
        Settings(
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            DRY_RUN=False,
            KILL_SWITCH=True,
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        )


def test_live_trading_cannot_be_enabled_in_dry_run() -> None:
    with pytest.raises(ValueError):
        Settings(
            LIVE_TRADING=True,
            SAFE_MODE=False,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            DRY_RUN=True,
            KILL_SWITCH=False,
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
        )


def test_settings_defaults_are_prod_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.kill_switch is True
    assert settings.dry_run is True
    assert settings.live_trading is False
    assert settings.safe_mode is True


def test_parse_symbols_csv_handles_whitespace_and_case() -> None:
    settings = Settings(SYMBOLS=" btc_try , Eth_Try,  sol_try ")

    assert settings.symbols == ["BTCTRY", "ETHTRY", "SOLTRY"]


def test_parse_degrade_warn_codes_csv() -> None:
    settings = Settings(DEGRADE_WARN_CODES_CSV="STALE_MARKET_DATA, ORDER_REJECT_SPIKE")
    assert settings.parsed_degrade_warn_codes() == {
        AnomalyCode.STALE_MARKET_DATA,
        AnomalyCode.ORDER_REJECT_SPIKE,
    }


def test_parse_degrade_warn_codes_invalid_raises() -> None:
    settings = Settings(DEGRADE_WARN_CODES_CSV="NOT_REAL")
    with pytest.raises(ValueError):
        settings.parsed_degrade_warn_codes()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("STALE_MARKET_DATA_SECONDS", 0),
        ("REJECT_SPIKE_THRESHOLD", 0),
        ("CURSOR_STALL_CYCLES", 0),
        ("DEGRADE_WARN_WINDOW_CYCLES", 0),
        ("DEGRADE_WARN_THRESHOLD", 0),
        ("CLOCK_SKEW_SECONDS_THRESHOLD", 0),
        ("LATENCY_SPIKE_MS", 0),
    ],
)
def test_invalid_anomaly_threshold_settings_raise(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_invalid_pnl_divergence_thresholds_raise() -> None:
    with pytest.raises(ValueError):
        Settings(PNL_DIVERGENCE_TRY_WARN=0)
    with pytest.raises(ValueError):
        Settings(PNL_DIVERGENCE_TRY_ERROR=0)
    with pytest.raises(ValueError):
        Settings(PNL_DIVERGENCE_TRY_WARN=100, PNL_DIVERGENCE_TRY_ERROR=50)


def test_stage7_enabled_requires_dry_run_and_no_live() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(STAGE7_ENABLED=True, DRY_RUN=False, LIVE_TRADING=False)

    with pytest.raises(ValidationError):
        Settings(STAGE7_ENABLED=True, DRY_RUN=True, LIVE_TRADING=True)


def test_stage4_planning_kernel_flag_defaults_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.stage4_use_planning_kernel is False


def test_market_data_mode_ws_requires_ws_enabled() -> None:
    with pytest.raises(ValueError, match="MARKET_DATA_MODE=ws requires BTCTURK_WS_ENABLED=true"):
        Settings(MARKET_DATA_MODE="ws", BTCTURK_WS_ENABLED=False)


def test_ws_market_data_rest_fallback_invalid_in_rest_mode() -> None:
    with pytest.raises(
        ValueError,
        match="WS_MARKET_DATA_REST_FALLBACK=true is only valid when MARKET_DATA_MODE=ws",
    ):
        Settings(MARKET_DATA_MODE="rest", WS_MARKET_DATA_REST_FALLBACK=True)


def test_max_market_data_age_ms_accepts_legacy_alias() -> None:
    settings = Settings(BTCTURK_MARKETDATA_MAX_AGE_MS=1234)
    assert settings.max_market_data_age_ms == 1234
