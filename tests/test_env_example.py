from __future__ import annotations

from pathlib import Path

from btcbot.config import Settings

EXPECTED_KEYS = {
    "BTCTURK_API_KEY",
    "BTCTURK_API_SECRET",
    "BTCTURK_BASE_URL",
    "KILL_SWITCH",
    "DRY_RUN",
    "LIVE_TRADING",
    "LIVE_TRADING_ACK",
    "TARGET_TRY",
    "OFFSET_BPS",
    "TTL_SECONDS",
    "MIN_ORDER_NOTIONAL_TRY",
    "STATE_DB_PATH",
    "DRY_RUN_TRY_BALANCE",
    "MAX_ORDERS_PER_CYCLE",
    "MAX_OPEN_ORDERS_PER_SYMBOL",
    "COOLDOWN_SECONDS",
    "NOTIONAL_CAP_TRY_PER_CYCLE",
    "MAX_NOTIONAL_PER_ORDER_TRY",
    "MIN_PROFIT_BPS",
    "MAX_POSITION_TRY_PER_SYMBOL",
    "ENABLE_AUTO_KILL_SWITCH",
    "LOG_LEVEL",
    "SYMBOLS",
}


def _env_lines() -> list[str]:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    return [
        line.strip()
        for line in env_example.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_env_example_is_multiline_and_key_value() -> None:
    lines = _env_lines()

    assert len(lines) > 1
    assert all("=" in line for line in lines)
    keys = {line.split("=", 1)[0] for line in lines}
    assert EXPECTED_KEYS.issubset(keys)


def test_env_example_values_load_into_settings(monkeypatch, tmp_path: Path) -> None:
    env_contents = Path(".env.example").read_text(encoding="utf-8")
    env_file = tmp_path / ".env.live"
    env_file.write_text(env_contents, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    for key in EXPECTED_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=str(env_file))

    assert settings.kill_switch is True
    assert settings.dry_run is True
    assert settings.target_try == 300
    assert settings.live_trading is False
    assert settings.live_trading_ack == "I_UNDERSTAND"
    assert settings.offset_bps == 20
    assert settings.ttl_seconds == 120
    assert settings.symbols == ["BTCTRY", "ETHTRY", "SOLTRY"]
    assert settings.state_db_path == "btcbot_state.db"
    assert settings.dry_run_try_balance == 1000
    assert settings.min_order_notional_try == 10
    assert settings.max_orders_per_cycle == 2
    assert settings.max_open_orders_per_symbol == 1
    assert settings.cooldown_seconds == 60
    assert settings.notional_cap_try_per_cycle == 1000
    assert settings.max_notional_per_order_try == 0
    assert settings.min_profit_bps == 30
    assert settings.max_position_try_per_symbol == 5000
    assert settings.enable_auto_kill_switch is True
    assert settings.log_level == "INFO"
    assert settings.btcturk_base_url == "https://api.btcturk.com"
