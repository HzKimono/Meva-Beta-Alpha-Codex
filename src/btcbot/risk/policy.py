from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from btcbot.domain.intent import Intent
from btcbot.domain.models import normalize_symbol
from btcbot.risk.exchange_rules import ExchangeRulesProvider

logger = logging.getLogger(__name__)


@dataclass
class RiskPolicyContext:
    cycle_id: str
    open_orders_by_symbol: dict[str, int]
    last_intent_ts_by_symbol_side: dict[tuple[str, str], datetime]
    mark_prices: dict[str, Decimal]


class RiskPolicy:
    def __init__(
        self,
        *,
        rules_provider: ExchangeRulesProvider,
        max_orders_per_cycle: int,
        max_open_orders_per_symbol: int,
        cooldown_seconds: int,
        notional_cap_try_per_cycle: Decimal,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.rules_provider = rules_provider
        self.max_orders_per_cycle = max_orders_per_cycle
        self.max_open_orders_per_symbol = max_open_orders_per_symbol
        self.cooldown_seconds = cooldown_seconds
        self.notional_cap_try_per_cycle = notional_cap_try_per_cycle
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    def evaluate(self, context: RiskPolicyContext, intents: list[Intent]) -> list[Intent]:
        if not intents:
            return []

        approved: list[Intent] = []
        used_notional = Decimal("0")

        for intent in intents[: self.max_orders_per_cycle]:
            symbol = normalize_symbol(intent.symbol)
            if context.open_orders_by_symbol.get(symbol, 0) >= self.max_open_orders_per_symbol:
                self._log_block(intent, "max_open_orders_per_symbol")
                continue

            last_ts = context.last_intent_ts_by_symbol_side.get((symbol, intent.side.value))
            if last_ts is not None:
                now = self.now_provider()
                age = (now - last_ts).total_seconds()
                if age < self.cooldown_seconds:
                    self._log_block(intent, "cooldown")
                    continue

            normalized = self._normalize_intent(intent)
            if normalized is None:
                continue

            price = normalized.limit_price
            if price is None:
                self._log_block(intent, "missing_limit_price")
                continue
            notional = normalized.qty * price
            if used_notional + notional > self.notional_cap_try_per_cycle:
                self._log_block(intent, "notional_cap")
                continue

            approved.append(normalized)
            used_notional += notional

        return approved

    def _normalize_intent(self, intent: Intent) -> Intent | None:
        if intent.limit_price is None:
            return intent
        rules = self.rules_provider.get_rules(intent.symbol)
        q_price = _quantize(intent.limit_price, rules.price_tick)
        q_qty = _quantize(intent.qty, rules.qty_step)
        if q_price <= 0 or q_qty <= 0:
            self._log_block(intent, "non_positive_after_quantize")
            return None
        if q_price * q_qty < rules.min_notional:
            self._log_block(intent, "min_notional")
            return None
        return Intent(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            qty=q_qty,
            limit_price=q_price,
            reason=intent.reason,
            confidence=intent.confidence,
            ttl_seconds=intent.ttl_seconds,
            idempotency_key=intent.idempotency_key,
            created_at=intent.created_at,
        )

    def _log_block(self, intent: Intent, reason: str) -> None:
        logger.info(
            "Intent blocked by risk policy",
            extra={
                "extra": {
                    "intent_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "reason": reason,
                }
            },
        )


def _quantize(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
