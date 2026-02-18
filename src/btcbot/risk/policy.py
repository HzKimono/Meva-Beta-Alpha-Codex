from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from btcbot.domain.decision_codes import ReasonCode, map_risk_reason
from btcbot.domain.intent import Intent
from btcbot.domain.models import normalize_symbol
from btcbot.observability_decisions import emit_decision
from btcbot.risk.exchange_rules import ExchangeRules, ExchangeRulesProvider

logger = logging.getLogger(__name__)


@dataclass
class RiskPolicyContext:
    cycle_id: str
    open_orders_by_symbol: dict[str, int]
    last_intent_ts_by_symbol_side: dict[tuple[str, str], datetime]
    mark_prices: dict[str, Decimal]
    cash_try_free: Decimal = Decimal("0")
    try_cash_target: Decimal = Decimal("0")
    investable_try: Decimal = Decimal("0")


class RiskPolicy:
    def __init__(
        self,
        *,
        rules_provider: ExchangeRulesProvider,
        max_orders_per_cycle: int,
        max_open_orders_per_symbol: int,
        cooldown_seconds: int,
        notional_cap_try_per_cycle: Decimal,
        max_notional_per_order_try: Decimal,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.rules_provider = rules_provider
        self.max_orders_per_cycle = max_orders_per_cycle
        self.max_open_orders_per_symbol = max_open_orders_per_symbol
        self.cooldown_seconds = cooldown_seconds
        self.notional_cap_try_per_cycle = notional_cap_try_per_cycle
        self.max_notional_per_order_try = max_notional_per_order_try
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    def evaluate(self, context: RiskPolicyContext, intents: list[Intent]) -> list[Intent]:
        if not intents:
            return []

        approved: list[Intent] = []
        used_notional = Decimal("0")

        if len(intents) > self.max_orders_per_cycle:
            emit_decision(
                logger,
                {
                    "cycle_id": context.cycle_id,
                    "decision_layer": "risk_policy",
                    "reason_code": str(ReasonCode.RISK_DROP_MAX_ORDERS_PER_CYCLE),
                    "action": "CAP",
                    "scope": "cycle_cumulative",
                    "rule_id": str(ReasonCode.RISK_DROP_MAX_ORDERS_PER_CYCLE),
                    "max_orders_per_cycle": str(self.max_orders_per_cycle),
                    "dropped_intents": str(max(0, len(intents) - self.max_orders_per_cycle)),
                },
            )

        for intent in intents[: self.max_orders_per_cycle]:
            symbol = normalize_symbol(intent.symbol)
            if context.open_orders_by_symbol.get(symbol, 0) >= self.max_open_orders_per_symbol:
                self._log_block(
                    intent, map_risk_reason("max_open_orders_per_symbol"), context=context
                )
                continue

            last_ts = context.last_intent_ts_by_symbol_side.get((symbol, intent.side.value))
            if last_ts is not None:
                now = self.now_provider()
                age = (now - last_ts).total_seconds()
                if age < self.cooldown_seconds:
                    self._log_block(intent, map_risk_reason("cooldown"), context=context)
                    continue

            normalized_result = self._normalize_intent(intent, context=context)
            if normalized_result is None:
                continue
            normalized, rules = normalized_result

            price = normalized.limit_price
            if price is None:
                self._log_block(intent, map_risk_reason("missing_limit_price"), context=context)
                continue
            notional = normalized.qty * price
            if context.investable_try > 0 and used_notional + notional > context.investable_try:
                self._log_block(
                    normalized,
                    map_risk_reason("cash_reserve_target"),
                    context=context,
                    intent_notional_try=notional,
                    used_notional_try=used_notional,
                )
                continue
            if self.max_notional_per_order_try > 0 and notional > self.max_notional_per_order_try:
                capped_qty = _quantize(self.max_notional_per_order_try / price, rules.qty_step)
                if capped_qty <= 0:
                    self._log_block(
                        normalized,
                        map_risk_reason("max_notional_per_order_cap_non_positive"),
                        context=context,
                        intent_notional_try=notional,
                    )
                    continue
                emit_decision(
                    logger,
                    {
                        "cycle_id": context.cycle_id,
                        "decision_layer": "risk_policy",
                        "reason_code": str(ReasonCode.RISK_CAP_MAX_NOTIONAL_PER_ORDER_TRY),
                        "action": "CAP",
                        "scope": "per_intent",
                        "intent_id": normalized.intent_id,
                        "symbol": normalized.symbol,
                        "side": normalized.side.value,
                        "rule_id": str(ReasonCode.RISK_CAP_MAX_NOTIONAL_PER_ORDER_TRY),
                        "max_notional_per_order_try": str(self.max_notional_per_order_try),
                    },
                )
                normalized = Intent(
                    intent_id=normalized.intent_id,
                    symbol=normalized.symbol,
                    side=normalized.side,
                    qty=capped_qty,
                    limit_price=normalized.limit_price,
                    reason=normalized.reason,
                    confidence=normalized.confidence,
                    ttl_seconds=normalized.ttl_seconds,
                    idempotency_key=normalized.idempotency_key,
                    created_at=normalized.created_at,
                )
                notional = normalized.qty * price
            if used_notional + notional > self.notional_cap_try_per_cycle:
                self._log_block(
                    normalized,
                    map_risk_reason("notional_cap"),
                    context=context,
                    intent_notional_try=notional,
                    used_notional_try=used_notional,
                )
                continue

            approved.append(normalized)
            used_notional += notional

        return approved

    def _normalize_intent(
        self,
        intent: Intent,
        *,
        context: RiskPolicyContext,
    ) -> tuple[Intent, ExchangeRules] | None:
        if intent.limit_price is None:
            return intent, ExchangeRules(
                min_notional=Decimal("0"),
                price_tick=Decimal("0"),
                qty_step=Decimal("0.00000001"),
            )
        rules = self.rules_provider.get_rules(intent.symbol)
        q_price = _quantize(intent.limit_price, rules.price_tick)
        q_qty = _quantize(intent.qty, rules.qty_step)
        if q_price <= 0 or q_qty <= 0:
            self._log_block(intent, map_risk_reason("non_positive_after_quantize"), context=context)
            return None
        if q_price * q_qty < rules.min_notional:
            self._log_block(intent, map_risk_reason("min_notional"), context=context)
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
        ), rules

    def _log_block(
        self,
        intent: Intent,
        reason: ReasonCode,
        *,
        context: RiskPolicyContext | None = None,
        intent_notional_try: Decimal | None = None,
        used_notional_try: Decimal | None = None,
    ) -> None:
        extra_payload: dict[str, object] = {
            "intent_id": intent.intent_id,
            "symbol": intent.symbol,
            "reason": str(reason),
            "reason_code": str(reason),
            "rule_id": str(reason),
            "cycle_id": context.cycle_id if context is not None else "",
            "decision_layer": "risk_policy",
            "action": "BLOCK",
            "scope": "per_intent",
            "side": intent.side.value,
        }
        if reason == ReasonCode.RISK_BLOCK_NOTIONAL_CAP:
            planned_spend_try = (used_notional_try or Decimal("0")) + (
                intent_notional_try or Decimal("0")
            )
            extra_payload.update(
                {
                    "rule": str(ReasonCode.RISK_BLOCK_NOTIONAL_CAP),
                    "cap_try_per_cycle": str(self.notional_cap_try_per_cycle),
                    "intent_notional_try": str(intent_notional_try or Decimal("0")),
                    "used_notional_try": str(used_notional_try or Decimal("0")),
                    "planned_spend_try": str(planned_spend_try),
                    "scope": "cycle_cumulative",
                }
            )
            if context is not None:
                extra_payload.update(
                    {
                        "cash_try_free": str(context.cash_try_free),
                        "try_cash_target": str(context.try_cash_target),
                        "investable_try": str(context.investable_try),
                    }
                )
        if reason == ReasonCode.RISK_BLOCK_CASH_RESERVE_TARGET and context is not None:
            extra_payload.update(
                {
                    "intent_notional_try": str(intent_notional_try or Decimal("0")),
                    "used_notional_try": str(used_notional_try or Decimal("0")),
                    "cash_try_free": str(context.cash_try_free),
                    "try_cash_target": str(context.try_cash_target),
                    "investable_try": str(context.investable_try),
                    "scope": "cycle_cumulative",
                }
            )
        logger.info(
            "Intent blocked by risk policy",
            extra={"extra": extra_payload},
        )
        emit_decision(logger, extra_payload)


def _quantize(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
