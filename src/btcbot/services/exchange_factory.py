from __future__ import annotations

import logging

from btcbot.adapters.btcturk_http import (
    BtcturkHttpClient,
    BtcturkHttpClientStage4,
    DryRunExchangeClient,
    DryRunExchangeClientStage4,
)
from btcbot.adapters.exchange import ExchangeClient
from btcbot.adapters.exchange_stage4 import ExchangeClientStage4
from btcbot.config import Settings
from btcbot.services.rate_limiter import EndpointBudget, TokenBucketRateLimiter
from btcbot.domain.models import Balance

logger = logging.getLogger(__name__)


def build_exchange_stage3(settings: Settings, *, force_dry_run: bool) -> ExchangeClient:
    dry_run = force_dry_run or settings.dry_run
    limiter = _build_rate_limiter(settings)
    if dry_run:
        public_client = BtcturkHttpClient(
            base_url=settings.btcturk_base_url,
            rate_limiter=limiter,
            breaker_429_consecutive_threshold=settings.breaker_429_consecutive_threshold,
            breaker_cooldown_seconds=settings.breaker_cooldown_seconds,
        )
        orderbooks: dict[str, tuple[float, float]] = {}
        exchange_info = []
        try:
            try:
                exchange_info = public_client.get_exchange_info()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not fetch exchange info in dry-run",
                    extra={
                        "extra": {
                            "error_type": type(exc).__name__,
                            "safe_message": "exchange info fetch failed",
                        }
                    },
                )
                exchange_info = []

            for symbol in sorted(settings.symbols):
                try:
                    orderbooks[symbol] = public_client.get_orderbook(symbol)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not fetch orderbook in dry-run",
                        extra={
                            "extra": {
                                "symbol": symbol,
                                "error_type": type(exc).__name__,
                                "safe_message": "orderbook fetch failed",
                            }
                        },
                    )
                    orderbooks[symbol] = (0.0, 0.0)
        finally:
            _close_best_effort(public_client, "public dry-run client")

        balances = [Balance(asset="TRY", free=settings.dry_run_try_balance)]
        return DryRunExchangeClient(
            balances=balances,
            orderbooks=orderbooks,
            exchange_info=exchange_info,
        )

    return BtcturkHttpClient(
        api_key=settings.btcturk_api_key.get_secret_value() if settings.btcturk_api_key else None,
        api_secret=settings.btcturk_api_secret.get_secret_value()
        if settings.btcturk_api_secret
        else None,
        base_url=settings.btcturk_base_url,
        rate_limiter=limiter,
        breaker_429_consecutive_threshold=settings.breaker_429_consecutive_threshold,
        breaker_cooldown_seconds=settings.breaker_cooldown_seconds,
    )


def build_exchange_stage4(settings: Settings, *, dry_run: bool) -> ExchangeClientStage4:
    if dry_run:
        dry_run_client = build_exchange_stage3(settings, force_dry_run=True)
        return DryRunExchangeClientStage4(dry_run_client)

    live_client = BtcturkHttpClient(
        api_key=settings.btcturk_api_key.get_secret_value() if settings.btcturk_api_key else None,
        api_secret=settings.btcturk_api_secret.get_secret_value()
        if settings.btcturk_api_secret
        else None,
        base_url=settings.btcturk_base_url,
        rate_limiter=_build_rate_limiter(settings),
        breaker_429_consecutive_threshold=settings.breaker_429_consecutive_threshold,
        breaker_cooldown_seconds=settings.breaker_cooldown_seconds,
    )
    return BtcturkHttpClientStage4(live_client)


def _close_best_effort(resource: object, label: str) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to close resource", extra={"extra": {"resource": label}}, exc_info=True
        )


def _build_rate_limiter(settings: Settings) -> TokenBucketRateLimiter:
    return TokenBucketRateLimiter(
        EndpointBudget(
            tokens_per_second=settings.rate_limit_marketdata_tps,
            burst_capacity=settings.rate_limit_marketdata_burst,
        ),
        group_budgets={
            "market_data": EndpointBudget(
                tokens_per_second=settings.rate_limit_marketdata_tps,
                burst_capacity=settings.rate_limit_marketdata_burst,
            ),
            "account": EndpointBudget(
                tokens_per_second=settings.rate_limit_account_tps,
                burst_capacity=settings.rate_limit_account_burst,
            ),
            "orders": EndpointBudget(
                tokens_per_second=settings.rate_limit_orders_tps,
                burst_capacity=settings.rate_limit_orders_burst,
            ),
        },
    )
