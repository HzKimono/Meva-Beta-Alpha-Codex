from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from httpx import HTTPStatusError

from btcbot.domain.symbols import canonical_symbol
from btcbot.services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)


class ExchangeRulesUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExchangeRules:
    min_notional: Decimal
    price_tick: Decimal
    qty_step: Decimal


class ExchangeRulesProvider(Protocol):
    def get_rules(self, symbol: str) -> ExchangeRules: ...


class MarketDataExchangeRulesProvider:
    def __init__(
        self,
        market_data_service: MarketDataService,
        *,
        cache_ttl_seconds: int = 600,
        now_provider: Callable[[], datetime] | None = None,
        allow_default_fallback: bool = True,
    ) -> None:
        self.market_data_service = market_data_service
        self.cache_ttl_seconds = cache_ttl_seconds
        self.now_provider = now_provider or (lambda: datetime.now(UTC))
        self._rules_cache: dict[str, tuple[datetime, ExchangeRules]] = {}
        self.allow_default_fallback = allow_default_fallback

    def get_rules(self, symbol: str) -> ExchangeRules:
        normalized = canonical_symbol(symbol)
        now = self.now_provider()
        cached = self._rules_cache.get(normalized)
        if cached is not None:
            fetched_at, rules = cached
            if now - fetched_at < timedelta(seconds=self.cache_ttl_seconds):
                return rules

        try:
            market_rules = self.market_data_service.get_symbol_rules(symbol)
            rules = ExchangeRules(
                min_notional=market_rules.min_total or Decimal("10"),
                price_tick=market_rules.tick_size or Decimal("0.01"),
                qty_step=market_rules.step_size or Decimal("0.00000001"),
            )
            self._rules_cache[normalized] = (now, rules)
            return rules
        except Exception as exc:  # noqa: BLE001
            extra: dict[str, object] = {
                "symbol": symbol,
                "normalized_symbol": normalized,
                "error_type": type(exc).__name__,
            }
            if isinstance(exc, HTTPStatusError) and exc.response is not None:
                extra["status_code"] = exc.response.status_code
            if not self.allow_default_fallback:
                logger.error(
                    "exchange_rules_missing_fail_closed",
                    extra={"extra": extra},
                    exc_info=True,
                )
                raise ExchangeRulesUnavailableError(
                    f"exchange_rules_missing_fail_closed:{normalized}"
                ) from exc
            logger.warning(
                "Exchange rules unavailable; using defaults",
                extra={"extra": extra},
                exc_info=True,
            )
            defaults = ExchangeRules(
                min_notional=Decimal("10"),
                price_tick=Decimal("0.01"),
                qty_step=Decimal("0.00000001"),
            )
            self._rules_cache[normalized] = (now, defaults)
            return defaults
