from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from btcbot.domain.anomalies import AnomalyCode
from btcbot.domain.symbols import canonical_symbol
from btcbot.domain.universe_models import UniverseKnobs


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.live",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    btcturk_api_key: SecretStr | None = Field(default=None, alias="BTCTURK_API_KEY")
    btcturk_api_secret: SecretStr | None = Field(default=None, alias="BTCTURK_API_SECRET")
    btcturk_api_scopes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["read", "trade"],
        alias="BTCTURK_API_SCOPES",
    )
    btcturk_secret_rotated_at: str | None = Field(default=None, alias="BTCTURK_SECRET_ROTATED_AT")
    btcturk_secret_max_age_days: int = Field(default=90, alias="BTCTURK_SECRET_MAX_AGE_DAYS")
    btcturk_base_url: str = Field(default="https://api.btcturk.com", alias="BTCTURK_BASE_URL")
    btcturk_ws_enabled: bool = Field(default=False, alias="BTCTURK_WS_ENABLED")
    btcturk_ws_url: str = Field(default="wss://ws-feed-pro.btcturk.com", alias="BTCTURK_WS_URL")
    btcturk_ws_idle_reconnect_ms: int = Field(default=30_000, alias="BTCTURK_WS_IDLE_RECONNECT_MS")
    btcturk_ws_queue_max: int = Field(default=1_000, alias="BTCTURK_WS_QUEUE_MAX")
    btcturk_rest_reliability_enabled: bool = Field(
        default=True,
        alias="BTCTURK_REST_RELIABILITY_ENABLED",
    )
    btcturk_rest_max_retries: int = Field(default=4, alias="BTCTURK_REST_MAX_RETRIES")
    btcturk_rest_base_delay_ms: int = Field(default=400, alias="BTCTURK_REST_BASE_DELAY_MS")
    btcturk_rest_max_delay_ms: int = Field(default=4_000, alias="BTCTURK_REST_MAX_DELAY_MS")
    btcturk_rate_limit_rps: float = Field(default=8.0, alias="BTCTURK_RATE_LIMIT_RPS")
    btcturk_rate_limit_burst: int = Field(default=8, alias="BTCTURK_RATE_LIMIT_BURST")
    rate_limit_marketdata_tps: float = Field(default=8.0, alias="RATE_LIMIT_MARKETDATA_TPS")
    rate_limit_marketdata_burst: int = Field(default=8, alias="RATE_LIMIT_MARKETDATA_BURST")
    rate_limit_account_tps: float = Field(default=4.0, alias="RATE_LIMIT_ACCOUNT_TPS")
    rate_limit_account_burst: int = Field(default=4, alias="RATE_LIMIT_ACCOUNT_BURST")
    rate_limit_orders_tps: float = Field(default=2.0, alias="RATE_LIMIT_ORDERS_TPS")
    rate_limit_orders_burst: int = Field(default=2, alias="RATE_LIMIT_ORDERS_BURST")
    breaker_429_consecutive_threshold: int = Field(
        default=3, alias="BREAKER_429_CONSECUTIVE_THRESHOLD"
    )
    breaker_cooldown_seconds: float = Field(default=3.0, alias="BREAKER_COOLDOWN_SECONDS")
    max_retry_attempts: int = Field(default=4, alias="MAX_RETRY_ATTEMPTS")
    max_retry_total_sleep_s: float = Field(default=8.0, alias="MAX_RETRY_TOTAL_SLEEP_S")
    btcturk_http_max_connections: int = Field(default=10, alias="BTCTURK_HTTP_MAX_CONNECTIONS")
    btcturk_clock_sync_interval_seconds: int = Field(
        default=60,
        alias="BTCTURK_CLOCK_SYNC_INTERVAL_SECONDS",
    )
    market_data_mode: str = Field(default="rest", alias="MARKET_DATA_MODE")
    max_market_data_age_ms: int = Field(
        default=15_000,
        validation_alias=AliasChoices("MAX_MARKET_DATA_AGE_MS", "BTCTURK_MARKETDATA_MAX_AGE_MS"),
    )
    ws_market_data_rest_fallback: bool = Field(
        default=False,
        alias="WS_MARKET_DATA_REST_FALLBACK",
    )
    orderbook_ttl_ms: int = Field(default=2000, alias="ORDERBOOK_TTL_MS")
    orderbook_max_staleness_ms: int = Field(
        default=5000,
        alias="ORDERBOOK_MAX_STALENESS_MS",
    )
    kill_switch: bool = Field(default=True, alias="KILL_SWITCH")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    live_trading: bool = Field(default=False, alias="LIVE_TRADING")
    live_trading_ack: str | None = Field(default=None, alias="LIVE_TRADING_ACK")

    target_try: float = Field(default=300.0, alias="TARGET_TRY")
    offset_bps: int = Field(default=20, alias="OFFSET_BPS")
    ttl_seconds: int = Field(default=120, alias="TTL_SECONDS")
    min_order_notional_try: float = Field(default=10.0, alias="MIN_ORDER_NOTIONAL_TRY")
    stage5_bootstrap_notional_try: Decimal = Field(
        default=Decimal("50"), alias="STAGE5_BOOTSTRAP_NOTIONAL_TRY"
    )
    stage5_max_intent_notional_try: Decimal = Field(
        default=Decimal("100"), alias="STAGE5_MAX_INTENT_NOTIONAL_TRY"
    )

    state_db_path: str = Field(default="btcbot_state.db", alias="STATE_DB_PATH")
    state_db_strict_lock: bool = Field(default=True, alias="STATE_DB_STRICT_LOCK")
    dry_run_try_balance: float = Field(default=1000.0, alias="DRY_RUN_TRY_BALANCE")
    max_orders_per_cycle: int = Field(default=2, alias="MAX_ORDERS_PER_CYCLE")
    max_open_orders_per_symbol: int = Field(default=1, alias="MAX_OPEN_ORDERS_PER_SYMBOL")
    cooldown_seconds: int = Field(default=60, alias="COOLDOWN_SECONDS")
    notional_cap_try_per_cycle: Decimal = Field(
        default=Decimal("1000"), alias="NOTIONAL_CAP_TRY_PER_CYCLE"
    )
    max_notional_per_order_try: Decimal = Field(
        default=Decimal("0"), alias="MAX_NOTIONAL_PER_ORDER_TRY"
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
    allocation_fee_buffer_bps: Decimal = Field(
        default=Decimal("20"), alias="ALLOCATION_FEE_BUFFER_BPS"
    )
    fee_buffer_ratio: Decimal = Field(default=Decimal("0"), alias="FEE_BUFFER_RATIO")
    investable_usage_mode: str = Field(default="use_all", alias="INVESTABLE_USAGE_MODE")
    investable_usage_fraction: Decimal = Field(
        default=Decimal("1"), alias="INVESTABLE_USAGE_FRACTION"
    )
    max_try_per_cycle: Decimal = Field(default=Decimal("0"), alias="MAX_TRY_PER_CYCLE")
    rules_cache_ttl_sec: int = Field(default=300, alias="RULES_CACHE_TTL_SEC")
    fills_poll_lookback_minutes: int = Field(default=30, alias="FILLS_POLL_LOOKBACK_MINUTES")
    stage4_bootstrap_intents: bool = Field(default=True, alias="STAGE4_BOOTSTRAP_INTENTS")
    stage4_use_planning_kernel: bool = Field(default=False, alias="STAGE4_USE_PLANNING_KERNEL")
    spot_sell_requires_inventory: bool = Field(
        default=True, alias="SPOT_SELL_REQUIRES_INVENTORY"
    )

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
    stage7_use_planning_kernel: bool = Field(default=True, alias="STAGE7_USE_PLANNING_KERNEL")
    stage7_rules_fallback_tick_size: Decimal = Field(
        default=Decimal("0.01"), alias="STAGE7_RULES_FALLBACK_TICK_SIZE"
    )
    stage7_rules_fallback_lot_size: Decimal = Field(
        default=Decimal("0.00000001"), alias="STAGE7_RULES_FALLBACK_LOT_SIZE"
    )
    stage7_rules_fallback_min_notional_try: Decimal = Field(
        default=Decimal("10"), alias="STAGE7_RULES_FALLBACK_MIN_NOTIONAL_TRY"
    )
    stage7_rules_safe_min_notional_try: Decimal = Field(
        default=Decimal("100"), alias="STAGE7_RULES_SAFE_MIN_NOTIONAL_TRY"
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
        default=Decimal("0.25"), alias="STAGE7_MAX_DRAWDOWN_PCT"
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
    stage7_sim_reject_prob_bps: Decimal = Field(
        default=Decimal("0"), alias="STAGE7_SIM_REJECT_PROB_BPS"
    )
    stage7_rate_limit_rps: float = Field(default=5.0, alias="STAGE7_RATE_LIMIT_RPS")
    stage7_rate_limit_burst: int = Field(default=5, alias="STAGE7_RATE_LIMIT_BURST")
    stage7_retry_max_attempts: int = Field(default=3, alias="STAGE7_RETRY_MAX_ATTEMPTS")
    stage7_retry_base_delay_ms: int = Field(default=100, alias="STAGE7_RETRY_BASE_DELAY_MS")
    stage7_retry_max_delay_ms: int = Field(default=1_000, alias="STAGE7_RETRY_MAX_DELAY_MS")

    stage7_reject_spike_threshold: int = Field(default=3, alias="STAGE7_REJECT_SPIKE_THRESHOLD")
    stage7_retry_alert_threshold: int = Field(default=5, alias="STAGE7_RETRY_ALERT_THRESHOLD")
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

    doctor_slo_enabled: bool = Field(default=True, alias="DOCTOR_SLO_ENABLED")
    doctor_slo_lookback: int = Field(default=20, alias="DOCTOR_SLO_LOOKBACK")
    doctor_slo_max_drawdown_ratio_warn: float = Field(
        default=0.10, alias="DOCTOR_SLO_MAX_DRAWDOWN_RATIO_WARN"
    )
    doctor_slo_max_drawdown_ratio_fail: float = Field(
        default=0.20, alias="DOCTOR_SLO_MAX_DRAWDOWN_RATIO_FAIL"
    )
    doctor_slo_max_reject_rate_warn: float = Field(
        default=0.05, alias="DOCTOR_SLO_MAX_REJECT_RATE_WARN"
    )
    doctor_slo_max_reject_rate_fail: float = Field(
        default=0.10, alias="DOCTOR_SLO_MAX_REJECT_RATE_FAIL"
    )
    doctor_slo_max_latency_ms_warn: int = Field(
        default=1000, alias="DOCTOR_SLO_MAX_LATENCY_MS_WARN"
    )
    doctor_slo_max_latency_ms_fail: int = Field(
        default=2000, alias="DOCTOR_SLO_MAX_LATENCY_MS_FAIL"
    )
    doctor_slo_min_fill_rate_warn: float = Field(
        default=0.90, alias="DOCTOR_SLO_MIN_FILL_RATE_WARN"
    )
    doctor_slo_min_fill_rate_fail: float = Field(
        default=0.80, alias="DOCTOR_SLO_MIN_FILL_RATE_FAIL"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    observability_enabled: bool = Field(default=False, alias="OBSERVABILITY_ENABLED")
    observability_metrics_exporter: str = Field(
        default="none", alias="OBSERVABILITY_METRICS_EXPORTER"
    )
    observability_otlp_endpoint: str | None = Field(
        default=None, alias="OBSERVABILITY_OTLP_ENDPOINT"
    )
    observability_prometheus_port: int = Field(default=9464, alias="OBSERVABILITY_PROMETHEUS_PORT")
    safe_mode: bool = Field(default=True, alias="SAFE_MODE")
    risk_balance_debug: bool = Field(default=False, alias="RISK_BALANCE_DEBUG")
    agent_policy_enabled: bool = Field(default=False, alias="AGENT_POLICY_ENABLED")
    agent_policy_provider: str = Field(default="rule", alias="AGENT_POLICY_PROVIDER")
    agent_observe_only: bool = Field(default=False, alias="AGENT_OBSERVE_ONLY")
    agent_symbol_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="AGENT_SYMBOL_ALLOWLIST",
    )
    agent_max_order_notional_try: Decimal = Field(
        default=Decimal("0"), alias="AGENT_MAX_ORDER_NOTIONAL_TRY"
    )
    agent_max_spread_bps: Decimal = Field(default=Decimal("120"), alias="AGENT_MAX_SPREAD_BPS")
    agent_prompt_capture_enabled: bool = Field(
        default=False,
        alias="AGENT_PROMPT_CAPTURE_ENABLED",
    )
    agent_prompt_capture_max_chars: int = Field(
        default=4000,
        alias="AGENT_PROMPT_CAPTURE_MAX_CHARS",
    )
    agent_llm_enabled: bool = Field(default=False, alias="AGENT_LLM_ENABLED")
    agent_llm_timeout_seconds: float = Field(default=3.0, alias="AGENT_LLM_TIMEOUT_SECONDS")
    agent_llm_model: str | None = Field(default=None, alias="AGENT_LLM_MODEL")
    agent_llm_provider: str = Field(default="none", alias="AGENT_LLM_PROVIDER")

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
    dynamic_universe_enabled: bool = Field(default=True, alias="DYNAMIC_UNIVERSE_ENABLED")
    universe_top_n: int = Field(default=5, alias="UNIVERSE_TOP_N")
    universe_spread_max_bps: Decimal = Field(default=Decimal("60"), alias="UNIVERSE_SPREAD_MAX_BPS")
    universe_min_depth_try: Decimal = Field(
        default=Decimal("50000"), alias="UNIVERSE_MIN_DEPTH_TRY"
    )
    universe_exclude_stables: bool = Field(default=True, alias="UNIVERSE_EXCLUDE_STABLES")
    universe_exclude_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["USDTTRY", "USDCTRY", "EURTRY"],
        alias="UNIVERSE_EXCLUDE_SYMBOLS",
    )
    universe_refresh_minutes: int = Field(default=60, alias="UNIVERSE_REFRESH_MINUTES")
    universe_history_bucket_minutes: int = Field(default=5, alias="UNIVERSE_HISTORY_BUCKET_MINUTES")
    universe_history_tolerance_minutes: int = Field(
        default=30,
        alias="UNIVERSE_HISTORY_TOLERANCE_MINUTES",
    )

    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTCTRY", "ETHTRY", "SOLTRY"],
        alias="SYMBOLS",
        validation_alias=AliasChoices("UNIVERSE_SYMBOLS", "SYMBOLS"),
    )
    universe_auto_correct: bool = Field(default=False, alias="UNIVERSE_AUTO_CORRECT")
    portfolio_targets: str | None = Field(default=None, alias="PORTFOLIO_TARGETS")

    @field_validator("symbols", mode="before")
    def parse_symbols(cls, value: str | list[str]) -> list[str]:
        return cls._parse_symbol_list(
            value,
            invalid_json_message="SYMBOLS JSON value must be a list",
        )

    @field_validator(
        "universe_allow_symbols",
        "universe_deny_symbols",
        "universe_exclude_symbols",
        "agent_symbol_allowlist",
        mode="before",
    )
    def parse_universe_symbols(cls, value: str | list[str]) -> list[str]:
        return cls._parse_symbol_list(
            value,
            invalid_json_message="UNIVERSE symbols JSON value must be a list",
        )

    @field_validator("btcturk_api_scopes", mode="before")
    def parse_btcturk_api_scopes(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("[") or raw.startswith("{"):
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("BTCTURK_API_SCOPES JSON value must be a list")
                values = parsed
            else:
                values = raw.split(",")
        else:
            values = value
        normalized: list[str] = []
        for item in values:
            token = str(item).strip().lower()
            if token:
                normalized.append(token)
        return normalized

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

    @field_validator("investable_usage_mode")
    def validate_investable_usage_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"use_all", "fraction", "cap"}:
            raise ValueError("INVESTABLE_USAGE_MODE must be one of: use_all,fraction,cap")
        return normalized

    @field_validator("investable_usage_fraction")
    def validate_investable_usage_fraction(cls, value: Decimal) -> Decimal:
        if value <= 0 or value > 1:
            raise ValueError("INVESTABLE_USAGE_FRACTION must be in (0, 1]")
        return value

    @field_validator("max_try_per_cycle")
    def validate_max_try_per_cycle(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("MAX_TRY_PER_CYCLE must be >= 0")
        return value

    @field_validator("allocation_fee_buffer_bps", "fee_buffer_ratio")
    def validate_allocation_fee_buffer_bps(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Fee buffer values must be >= 0")
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

    @field_validator("market_data_mode")
    def validate_market_data_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"rest", "ws"}:
            raise ValueError("MARKET_DATA_MODE must be one of: rest,ws")
        return normalized

    @field_validator("max_market_data_age_ms")
    def validate_max_market_data_age_ms(cls, value: int) -> int:
        if value < 1:
            raise ValueError("MAX_MARKET_DATA_AGE_MS must be >= 1")
        return value

    @field_validator("stage7_score_weights", mode="before")
    def validate_stage7_score_weights(
        cls, value: str | dict[str, object] | None
    ) -> dict[str, float] | None:
        if value is None:
            return None
        parsed: object = value
        if isinstance(value, str):
            token = value.strip()
            if not token:
                return None
            try:
                parsed = json.loads(token)
            except json.JSONDecodeError as exc:
                raise ValueError("STAGE7_SCORE_WEIGHTS must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("STAGE7_SCORE_WEIGHTS must be a dict[str,float]")
        normalized: dict[str, float] = {}
        for key, weight in parsed.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("STAGE7_SCORE_WEIGHTS keys must be non-empty strings")
            try:
                normalized[key] = float(weight)
            except (TypeError, ValueError) as exc:
                raise ValueError("STAGE7_SCORE_WEIGHTS values must be numeric") from exc
        return normalized

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
        "universe_top_n",
        "universe_refresh_minutes",
        "universe_history_bucket_minutes",
        "universe_history_tolerance_minutes",
    )
    def validate_dynamic_universe_positive_ints(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Dynamic universe integer settings must be >= 1")
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

    @field_validator("universe_spread_max_bps", "universe_min_depth_try")
    def validate_dynamic_universe_non_negative_decimals(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Dynamic universe decimal settings must be >= 0")
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

    @field_validator("stage7_max_drawdown_pct")
    def validate_stage7_max_drawdown_pct(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 1:
            raise ValueError("STAGE7_MAX_DRAWDOWN_PCT must be in [0, 1]")
        return value

    @field_validator("stage7_max_daily_loss_try")
    def validate_stage7_max_daily_loss_try(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Stage7 risk decimal settings must be >= 0")
        return value

    @field_validator(
        "stage7_spread_spike_bps",
        "stage7_risk_cooldown_sec",
        "stage7_retry_max_attempts",
        "stage7_retry_base_delay_ms",
        "stage7_retry_max_delay_ms",
        "stage7_rate_limit_burst",
        "stage7_reject_spike_threshold",
        "stage7_retry_alert_threshold",
    )
    def validate_stage7_risk_non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Stage7 risk integer settings must be >= 0")
        return value

    @field_validator("stage7_rate_limit_rps", mode="before")
    def validate_stage7_rate_limit_rps(cls, value: object) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("STAGE7_RATE_LIMIT_RPS must be numeric") from exc
        if parsed <= 0:
            raise ValueError("STAGE7_RATE_LIMIT_RPS must be > 0")
        return parsed

    @field_validator(
        "stage7_max_consecutive_losses",
        "stage7_max_data_age_sec",
        "stage7_concentration_top_n",
    )
    def validate_stage7_risk_min_one_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Stage7 risk integer settings must be >= 1")
        return value

    @field_validator("doctor_slo_lookback")
    def validate_doctor_slo_lookback(cls, value: int) -> int:
        if value < 1:
            raise ValueError("DOCTOR_SLO_LOOKBACK must be >= 1")
        return value

    @field_validator(
        "doctor_slo_max_drawdown_ratio_warn",
        "doctor_slo_max_drawdown_ratio_fail",
        "doctor_slo_max_reject_rate_warn",
        "doctor_slo_max_reject_rate_fail",
        "doctor_slo_min_fill_rate_warn",
        "doctor_slo_min_fill_rate_fail",
    )
    def validate_doctor_slo_rate_range(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("Doctor SLO rate thresholds must be in [0, 1]")
        return value

    @field_validator("doctor_slo_max_latency_ms_warn", "doctor_slo_max_latency_ms_fail")
    def validate_doctor_slo_latency(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Doctor SLO latency thresholds must be >= 0")
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
    def validate_market_data_settings(self) -> Settings:
        if self.market_data_mode == "ws" and not self.btcturk_ws_enabled:
            raise ValueError("MARKET_DATA_MODE=ws requires BTCTURK_WS_ENABLED=true")
        if self.market_data_mode == "rest" and self.ws_market_data_rest_fallback:
            raise ValueError(
                "WS_MARKET_DATA_REST_FALLBACK=true is only valid when MARKET_DATA_MODE=ws"
            )
        return self

    @model_validator(mode="after")
    def validate_stage7_safety(self) -> Settings:
        if self.stage7_enabled and (not self.dry_run or self.live_trading):
            raise ValueError("STAGE7_ENABLED requires DRY_RUN=true and LIVE_TRADING=false")

        if self.dry_run and self.live_trading:
            raise ValueError("LIVE_TRADING=true cannot be combined with DRY_RUN=true")

        if self.live_trading:
            if self.live_trading_ack != "I_UNDERSTAND":
                raise ValueError("LIVE_TRADING=true requires LIVE_TRADING_ACK=I_UNDERSTAND")
            if self.kill_switch:
                raise ValueError("LIVE_TRADING=true requires KILL_SWITCH=false")
            if self.safe_mode:
                raise ValueError("LIVE_TRADING=true requires SAFE_MODE=false")
            if self.btcturk_api_key is None or self.btcturk_api_secret is None:
                raise ValueError(
                    "LIVE_TRADING=true requires BTCTURK_API_KEY and BTCTURK_API_SECRET"
                )

        self.log_level = self.log_level.strip().upper()
        if self.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG")
        return self

    @model_validator(mode="after")
    def validate_doctor_slo_threshold_order(self) -> Settings:
        if self.doctor_slo_max_drawdown_ratio_warn > self.doctor_slo_max_drawdown_ratio_fail:
            raise ValueError(
                "DOCTOR_SLO_MAX_DRAWDOWN_RATIO_WARN must be <= DOCTOR_SLO_MAX_DRAWDOWN_RATIO_FAIL"
            )
        if self.doctor_slo_max_reject_rate_warn > self.doctor_slo_max_reject_rate_fail:
            raise ValueError(
                "DOCTOR_SLO_MAX_REJECT_RATE_WARN must be <= DOCTOR_SLO_MAX_REJECT_RATE_FAIL"
            )
        if self.doctor_slo_max_latency_ms_warn > self.doctor_slo_max_latency_ms_fail:
            raise ValueError(
                "DOCTOR_SLO_MAX_LATENCY_MS_WARN must be <= DOCTOR_SLO_MAX_LATENCY_MS_FAIL"
            )
        if self.doctor_slo_min_fill_rate_warn < self.doctor_slo_min_fill_rate_fail:
            raise ValueError(
                "DOCTOR_SLO_MIN_FILL_RATE_WARN must be >= DOCTOR_SLO_MIN_FILL_RATE_FAIL"
            )
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

    def is_safe_mode_enabled(self) -> bool:
        return self.safe_mode

    def symbols_source(self) -> str:
        """Return the highest-precedence source for configured symbols."""
        if os.getenv("UNIVERSE_SYMBOLS") is not None:
            return "env:UNIVERSE_SYMBOLS"
        if os.getenv("SYMBOLS") is not None:
            return "env:SYMBOLS"

        env_file = self.model_config.get("env_file")
        env_path = Path(str(env_file)) if env_file else None
        if env_path is not None and env_path.exists():
            found = _find_dotenv_key(env_path, keys=("UNIVERSE_SYMBOLS", "SYMBOLS"))
            if found is not None:
                return f"dotenv:{env_path}:{found}"
        return "default"


def _find_dotenv_key(path: Path, *, keys: tuple[str, ...]) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        for key in keys:
            if stripped.startswith(f"{key}="):
                return key
    return None
