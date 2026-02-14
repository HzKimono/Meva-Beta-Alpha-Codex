from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from btcbot.domain.anomalies import AnomalyCode
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

    stage7_enabled: bool = Field(default=False, alias="STAGE7_ENABLED")
    stage7_slippage_bps: Decimal = Field(default=Decimal("25"), alias="STAGE7_SLIPPAGE_BPS")
    stage7_fees_bps: Decimal = Field(default=Decimal("20"), alias="STAGE7_FEES_BPS")
    stage7_mark_price_source: str = Field(default="mid", alias="STAGE7_MARK_PRICE_SOURCE")
    stage7_universe_size: int = Field(default=20, alias="STAGE7_UNIVERSE_SIZE")
    stage7_universe_quote_ccy: str = Field(default="TRY", alias="STAGE7_UNIVERSE_QUOTE_CCY")
    stage7_universe_whitelist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="STAGE7_UNIVERSE_WHITELIST",
    )
    stage7_universe_blacklist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="STAGE7_UNIVERSE_BLACKLIST",
    )
    stage7_min_quote_volume_try: Decimal = Field(
        default=Decimal("0"),
        alias="STAGE7_MIN_QUOTE_VOLUME_TRY",
    )
    stage7_max_spread_bps: Decimal = Field(
        default=Decimal("1000000"),
        alias="STAGE7_MAX_SPREAD_BPS",
    )
    stage7_vol_lookback: int = Field(default=20, alias="STAGE7_VOL_LOOKBACK")
    stage7_score_weights: dict[str, float] | None = Field(
        default=None, alias="STAGE7_SCORE_WEIGHTS"
    )
    stage7_order_offset_bps: Decimal = Field(default=Decimal("5"), alias="STAGE7_ORDER_OFFSET_BPS")
    stage7_rules_fallback_tick_size: Decimal = Field(
        default=Decimal("0.01"), alias="STAGE7_RULES_FALLBACK_TICK_SIZE"
    )
    stage7_rules_fallback_lot_size: Decimal = Field(
        default=Decimal("0.00000001"), alias="STAGE7_RULES_FALLBACK_LOT_SIZE"
    )
    stage7_rules_fallback_min_notional_try: Decimal = Field(
        default=Decimal("10"), alias="STAGE7_RULES_FALLBACK_MIN_NOTIONAL_TRY"
    )
    stage7_rules_require_metadata: bool = Field(
        default=True,
        alias="STAGE7_RULES_REQUIRE_METADATA",
    )
    stage7_rules_invalid_metadata_policy: str = Field(
        default="skip_symbol",
        alias="STAGE7_RULES_INVALID_METADATA_POLICY",
    )
    stage7_max_drawdown_pct: Decimal = Field(
        default=Decimal("0.20"), alias="STAGE7_MAX_DRAWDOWN_PCT"
    )
    stage7_max_daily_loss_try: Decimal = Field(
        default=Decimal("500"), alias="STAGE7_MAX_DAILY_LOSS_TRY"
    )
    stage7_max_consecutive_losses: int = Field(default=3, alias="STAGE7_MAX_CONSECUTIVE_LOSSES")
    stage7_max_data_age_sec: int = Field(default=60, alias="STAGE7_MAX_DATA_AGE_SEC")
    stage7_spread_spike_bps: int = Field(default=300, alias="STAGE7_SPREAD_SPIKE_BPS")
    stage7_risk_cooldown_sec: int = Field(default=900, alias="STAGE7_RISK_COOLDOWN_SEC")
    stage7_concentration_top_n: int = Field(default=3, alias="STAGE7_CONCENTRATION_TOP_N")
    stage7_loss_guardrail_mode: str = Field(
        default="reduce_risk_only", alias="STAGE7_LOSS_GUARDRAIL_MODE"
    )

    risk_max_daily_drawdown_try: Decimal = Field(
        default=Decimal("1000"), alias="RISK_MAX_DAILY_DRAWDOWN_TRY"
    )
    risk_max_drawdown_try: Decimal = Field(default=Decimal("3000"), alias="RISK_MAX_DRAWDOWN_TRY")
    risk_max_gross_exposure_try: Decimal = Field(
        default=Decimal("10000"), alias="RISK_MAX_GROSS_EXPOSURE_TRY"
    )
    risk_max_position_pct: Decimal = Field(default=Decimal("0.30"), alias="RISK_MAX_POSITION_PCT")
    risk_max_order_notional_try: Decimal = Field(
        default=Decimal("3000"), alias="RISK_MAX_ORDER_NOTIONAL_TRY"
    )
    risk_min_cash_try: Decimal | None = Field(default=None, alias="RISK_MIN_CASH_TRY")
    risk_max_fee_try_per_day: Decimal | None = Field(default=None, alias="RISK_MAX_FEE_TRY_PER_DAY")

    stale_market_data_seconds: int = Field(default=30, alias="STALE_MARKET_DATA_SECONDS")
    reject_spike_threshold: int = Field(default=3, alias="REJECT_SPIKE_THRESHOLD")
    latency_spike_ms: int | None = Field(default=2000, alias="LATENCY_SPIKE_MS")
    cursor_stall_cycles: int = Field(default=5, alias="CURSOR_STALL_CYCLES")
    pnl_divergence_try_warn: Decimal = Field(default=Decimal("50"), alias="PNL_DIVERGENCE_TRY_WARN")
    pnl_divergence_try_error: Decimal = Field(
        default=Decimal("200"), alias="PNL_DIVERGENCE_TRY_ERROR"
    )
    degrade_warn_window_cycles: int = Field(default=10, alias="DEGRADE_WARN_WINDOW_CYCLES")
    degrade_warn_threshold: int = Field(default=3, alias="DEGRADE_WARN_THRESHOLD")
    degrade_warn_codes_csv: str = Field(
        default="STALE_MARKET_DATA,ORDER_REJECT_SPIKE,PNL_DIVERGENCE",
        alias="DEGRADE_WARN_CODES_CSV",
    )
    clock_skew_seconds_threshold: int = Field(default=30, alias="CLOCK_SKEW_SECONDS_THRESHOLD")

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

    @field_validator("symbols", mode="before")
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

    @field_validator("stage7_universe_whitelist", "stage7_universe_blacklist", mode="before")
    def parse_stage7_universe_symbols(cls, value: str | list[str]) -> list[str]:
        return cls._parse_symbol_list(
            value,
            invalid_json_message="STAGE7_UNIVERSE symbols JSON value must be a list",
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

    @field_validator(
        "risk_max_daily_drawdown_try",
        "risk_max_drawdown_try",
        "risk_max_gross_exposure_try",
        "risk_max_order_notional_try",
    )
    def validate_positive_risk_try_limits(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("Risk TRY limits must be > 0")
        return value

    @field_validator("risk_max_position_pct")
    def validate_risk_max_position_pct(cls, value: Decimal) -> Decimal:
        if value <= 0 or value > 1:
            raise ValueError("RISK_MAX_POSITION_PCT must be in (0, 1]")
        return value

    @field_validator("risk_max_fee_try_per_day")
    def validate_risk_max_fee_try_per_day(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("RISK_MAX_FEE_TRY_PER_DAY must be > 0 when configured")
        return value

    @field_validator(
        "stale_market_data_seconds",
        "clock_skew_seconds_threshold",
    )
    def validate_positive_seconds_thresholds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Anomaly second thresholds must be > 0")
        return value

    @field_validator(
        "reject_spike_threshold",
        "cursor_stall_cycles",
        "degrade_warn_window_cycles",
        "degrade_warn_threshold",
    )
    def validate_min_one_anomaly_counts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Anomaly count thresholds must be >= 1")
        return value

    @field_validator("latency_spike_ms")
    def validate_latency_spike_ms(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("LATENCY_SPIKE_MS must be >= 1 when configured")
        return value

    @field_validator("pnl_divergence_try_warn", "pnl_divergence_try_error")
    def validate_positive_pnl_divergence_thresholds(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("PnL divergence thresholds must be > 0")
        return value

    @field_validator("pnl_divergence_try_error")
    def validate_pnl_divergence_error_not_less_than_warn(cls, value: Decimal, info) -> Decimal:
        warn_value = info.data.get("pnl_divergence_try_warn")
        if isinstance(warn_value, Decimal) and value < warn_value:
            raise ValueError("PNL_DIVERGENCE_TRY_ERROR must be >= PNL_DIVERGENCE_TRY_WARN")
        return value

    @field_validator("stage7_slippage_bps", "stage7_fees_bps")
    def validate_stage7_non_negative_bps(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Stage7 bps values must be >= 0")
        return value

    @field_validator("stage7_mark_price_source")
    def validate_stage7_mark_source(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"mid", "last"}:
            raise ValueError("STAGE7_MARK_PRICE_SOURCE must be one of: mid,last")
        return normalized

    @field_validator("stage7_universe_size", "stage7_vol_lookback")
    def validate_stage7_positive_ints(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Stage7 universe integer settings must be >= 1")
        return value

    @field_validator(
        "stage7_min_quote_volume_try",
        "stage7_max_spread_bps",
        "stage7_order_offset_bps",
        "stage7_rules_fallback_tick_size",
        "stage7_rules_fallback_lot_size",
        "stage7_rules_fallback_min_notional_try",
    )
    def validate_stage7_non_negative_decimals(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Stage7 universe decimal settings must be >= 0")
        return value

    @field_validator("stage7_universe_quote_ccy")
    def validate_stage7_quote_ccy(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("STAGE7_UNIVERSE_QUOTE_CCY must be non-empty")
        return normalized

    @field_validator("stage7_rules_invalid_metadata_policy")
    def validate_stage7_rules_invalid_metadata_policy(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"skip_symbol", "observe_only_cycle"}:
            raise ValueError(
                "STAGE7_RULES_INVALID_METADATA_POLICY must be one of: "
                "skip_symbol,observe_only_cycle"
            )
        return normalized

    @field_validator(
        "stage7_max_drawdown_pct",
        "stage7_max_daily_loss_try",
    )
    def validate_stage7_risk_non_negative_decimal(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Stage7 risk decimal settings must be >= 0")
        return value

    @field_validator(
        "stage7_max_consecutive_losses",
        "stage7_max_data_age_sec",
        "stage7_spread_spike_bps",
        "stage7_risk_cooldown_sec",
        "stage7_concentration_top_n",
    )
    def validate_stage7_risk_non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Stage7 risk integer settings must be >= 0")
        return value

    @field_validator("stage7_loss_guardrail_mode")
    def validate_stage7_loss_guardrail_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"reduce_risk_only", "observe_only"}:
            raise ValueError(
                "STAGE7_LOSS_GUARDRAIL_MODE must be one of: reduce_risk_only,observe_only"
            )
        return normalized

    @model_validator(mode="after")
    def validate_stage7_safety(self) -> Settings:
        if self.stage7_enabled and (not self.dry_run or self.live_trading):
            raise ValueError("STAGE7_ENABLED requires DRY_RUN=true and LIVE_TRADING=false")
        return self

    def parsed_degrade_warn_codes(self) -> set[AnomalyCode]:
        parsed_codes: set[AnomalyCode] = set()
        for raw in self.degrade_warn_codes_csv.split(","):
            token = raw.strip().upper()
            if not token:
                continue
            try:
                parsed_codes.add(AnomalyCode(token))
            except ValueError as exc:
                raise ValueError(f"Unknown degrade warn code: {token}") from exc
        return parsed_codes

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
