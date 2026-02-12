from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from btcbot.domain.symbols import canonical_symbol
from btcbot.domain.universe_models import UniverseKnobs


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    btcturk_api_key: SecretStr | None = Field(default=None, alias="BTCTURK_API_KEY")
    btcturk_api_secret: SecretStr | None = Field(default=None, alias="BTCTURK_API_SECRET")
    btcturk_base_url: str = Field(default="https://api.btcturk.com", alias="BTCTURK_BASE_URL")

    kill_switch: bool = Field(default=True, alias="KILL_SWITCH")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    live_trading: bool = Field(default=False, alias="LIVE_TRADING")
    live_trading_ack: str | None = Field(default=None, alias="LIVE_TRADING_ACK")

    target_try: float = Field(default=300.0, alias="TARGET_TRY")
    offset_bps: int = Field(default=20, alias="OFFSET_BPS")
    ttl_seconds: int = Field(default=120, alias="TTL_SECONDS")
    min_order_notional_try: float = Field(default=10.0, alias="MIN_ORDER_NOTIONAL_TRY")

    state_db_path: str = Field(default="btcbot_state.db", alias="STATE_DB_PATH")
    dry_run_try_balance: float = Field(default=1000.0, alias="DRY_RUN_TRY_BALANCE")
    max_orders_per_cycle: int = Field(default=2, alias="MAX_ORDERS_PER_CYCLE")
    max_open_orders_per_symbol: int = Field(default=1, alias="MAX_OPEN_ORDERS_PER_SYMBOL")
    cooldown_seconds: int = Field(default=60, alias="COOLDOWN_SECONDS")
    notional_cap_try_per_cycle: Decimal = Field(
        default=Decimal("1000"), alias="NOTIONAL_CAP_TRY_PER_CYCLE"
    )
    min_profit_bps: int = Field(default=30, alias="MIN_PROFIT_BPS")
    max_position_try_per_symbol: Decimal = Field(
        default=Decimal("5000"), alias="MAX_POSITION_TRY_PER_SYMBOL"
    )
    enable_auto_kill_switch: bool = Field(default=True, alias="ENABLE_AUTO_KILL_SWITCH")

    max_open_orders: int = Field(default=5, alias="MAX_OPEN_ORDERS")
    max_position_notional_try: Decimal = Field(
        default=Decimal("5000"), alias="MAX_POSITION_NOTIONAL_TRY"
    )
    max_daily_loss_try: Decimal = Field(default=Decimal("1000"), alias="MAX_DAILY_LOSS_TRY")
    max_drawdown_pct: Decimal = Field(default=Decimal("10"), alias="MAX_DRAWDOWN_PCT")
    fee_bps_maker: Decimal = Field(default=Decimal("10"), alias="FEE_BPS_MAKER")
    fee_bps_taker: Decimal = Field(default=Decimal("15"), alias="FEE_BPS_TAKER")
    slippage_bps_buffer: Decimal = Field(default=Decimal("10"), alias="SLIPPAGE_BPS_BUFFER")
    try_cash_target: Decimal = Field(default=Decimal("300"), alias="TRY_CASH_TARGET")
    try_cash_max: Decimal = Field(default=Decimal("600"), alias="TRY_CASH_MAX")
    rules_cache_ttl_sec: int = Field(default=300, alias="RULES_CACHE_TTL_SEC")
    fills_poll_lookback_minutes: int = Field(default=30, alias="FILLS_POLL_LOOKBACK_MINUTES")
    stage4_bootstrap_intents: bool = Field(default=True, alias="STAGE4_BOOTSTRAP_INTENTS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    universe_quote_currency: str = Field(default="TRY", alias="UNIVERSE_QUOTE_CURRENCY")
    universe_max_size: int = Field(default=20, alias="UNIVERSE_MAX_SIZE")
    universe_min_notional_try: Decimal = Field(
        default=Decimal("50"), alias="UNIVERSE_MIN_NOTIONAL_TRY"
    )
    universe_max_spread_bps: Decimal = Field(
        default=Decimal("200"), alias="UNIVERSE_MAX_SPREAD_BPS"
    )
    universe_max_exchange_min_total_try: Decimal = Field(
        default=Decimal("1000000"), alias="UNIVERSE_MAX_EXCHANGE_MIN_TOTAL_TRY"
    )
    universe_allow_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="UNIVERSE_ALLOW_SYMBOLS"
    )
    universe_deny_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="UNIVERSE_DENY_SYMBOLS"
    )
    universe_require_active: bool = Field(default=True, alias="UNIVERSE_REQUIRE_ACTIVE")
    universe_require_try_quote: bool = Field(default=True, alias="UNIVERSE_REQUIRE_TRY_QUOTE")

    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTCTRY", "ETHTRY", "SOLTRY"],
        alias="SYMBOLS",
    )

    @field_validator("symbols", "universe_allow_symbols", "universe_deny_symbols", mode="before")
    def parse_symbols(cls, value: str | list[str]) -> list[str]:
        return cls._parse_symbol_list(
            value,
            invalid_json_message="SYMBOLS JSON value must be a list",
        )

    @field_validator("universe_allow_symbols", "universe_deny_symbols", mode="before")
    def parse_universe_symbols(cls, value: str | list[str]) -> list[str]:
        return cls._parse_symbol_list(
            value,
            invalid_json_message="UNIVERSE symbols JSON value must be a list",
        )

    @classmethod
    def _parse_symbol_list(
        cls,
        value: str | list[str],
        *,
        invalid_json_message: str,
    ) -> list[str]:
        items: list[object]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("[") or raw.startswith("{"):
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError(invalid_json_message)
                items = parsed
            else:
                items = raw.split(",")
        else:
            items = value

        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            candidate = cls._normalize_symbol(item)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)
        return normalized

    @staticmethod
    def _normalize_symbol(value: object) -> str:
        if value is None:
            return ""
        cleaned = str(value).strip().strip("[]").strip('"').strip("'").strip()
        if not cleaned:
            return ""
        return canonical_symbol(cleaned)

    @field_validator("target_try")
    def validate_target_try(cls, value: float) -> float:
        if value < 0:
            raise ValueError("TARGET_TRY must be >= 0")
        return value

    @field_validator("offset_bps")
    def validate_offset_bps(cls, value: int) -> int:
        if value < 0:
            raise ValueError("OFFSET_BPS must be >= 0")
        return value

    @field_validator("ttl_seconds")
    def validate_ttl_seconds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("TTL_SECONDS must be > 0")
        return value

    @field_validator("min_order_notional_try")
    def validate_min_order_notional_try(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("MIN_ORDER_NOTIONAL_TRY must be > 0")
        return value

    @field_validator("dry_run_try_balance")
    def validate_dry_run_try_balance(cls, value: float) -> float:
        if value < 0:
            raise ValueError("DRY_RUN_TRY_BALANCE must be >= 0")
        return value

    @field_validator("cooldown_seconds")
    def validate_cooldown_seconds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("COOLDOWN_SECONDS must be >= 0")
        return value

    def universe_knobs(self) -> UniverseKnobs:
        return UniverseKnobs(
            quote_currency=self.universe_quote_currency,
            max_universe_size=self.universe_max_size,
            min_notional_try=self.universe_min_notional_try,
            max_spread_bps=self.universe_max_spread_bps,
            max_exchange_min_total_try=self.universe_max_exchange_min_total_try,
            allow_symbols=tuple(self.universe_allow_symbols),
            deny_symbols=tuple(self.universe_deny_symbols),
            require_active=self.universe_require_active,
            require_try_quote=self.universe_require_try_quote,
        )

    def is_live_trading_enabled(self) -> bool:
        return self.live_trading and self.live_trading_ack == "I_UNDERSTAND"
