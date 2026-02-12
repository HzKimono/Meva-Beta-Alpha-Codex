from __future__ import annotations

import hashlib
import json
import logging
from decimal import ROUND_DOWN, Decimal

from btcbot.domain.models import Balance, OrderIntent, SymbolInfo
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)

EIGHT_DP = Decimal("0.00000001")
TWO_DP = Decimal("0.01")


class SweepService:
    def __init__(
        self,
        state_store: StateStore,
        target_try: float = 300.0,
        offset_bps: int = 20,
        default_min_notional: float = 10.0,
    ) -> None:
        self.state_store = state_store
        self.target_try = Decimal(str(target_try))
        self.offset_bps = Decimal(str(offset_bps))
        self.default_min_notional = Decimal(str(default_min_notional))

    def build_order_intents(
        self,
        cycle_id: str,
        balances: list[Balance],
        symbols: list[str],
        best_bids: dict[str, float],
        symbol_rules: dict[str, SymbolInfo] | None = None,
    ) -> list[OrderIntent]:
        symbol_rules = symbol_rules or {}
        free_try = self._try_free_balance(balances)
        excess = max(Decimal("0"), free_try - self.target_try)
        if excess <= 0:
            return []

        sorted_symbols = sorted(set(symbols))
        if not sorted_symbols:
            return []

        intents_by_symbol: dict[str, OrderIntent] = {}
        used_notional = Decimal("0")

        per_symbol_budget = (excess / Decimal(str(len(sorted_symbols)))).quantize(
            EIGHT_DP,
            rounding=ROUND_DOWN,
        )
        for symbol in sorted_symbols:
            remaining = max(Decimal("0"), excess - used_notional)
            if remaining < self.default_min_notional:
                break

            intent = self._intent_for_budget(
                cycle_id=cycle_id,
                symbol=symbol,
                budget=min(per_symbol_budget, remaining),
                best_bids=best_bids,
                symbol_rules=symbol_rules,
            )
            if intent is None:
                continue

            intents_by_symbol[symbol] = intent
            used_notional += Decimal(str(intent.notional))

        round_robin_index = 0
        while True:
            remaining = max(Decimal("0"), excess - used_notional)
            if remaining < self.default_min_notional:
                break

            symbol = sorted_symbols[round_robin_index % len(sorted_symbols)]
            round_robin_index += 1

            chunk_budget = min(per_symbol_budget, remaining)
            intent = self._intent_for_budget(
                cycle_id=cycle_id,
                symbol=symbol,
                budget=chunk_budget,
                best_bids=best_bids,
                symbol_rules=symbol_rules,
            )
            if intent is None:
                if round_robin_index >= len(sorted_symbols) * 4:
                    break
                continue

            if symbol in intents_by_symbol:
                current = intents_by_symbol[symbol]
                current_qty = Decimal(str(current.quantity)) + Decimal(str(intent.quantity))
                current_notional = Decimal(str(current.notional)) + Decimal(str(intent.notional))
                current.quantity = float(current_qty.quantize(EIGHT_DP, rounding=ROUND_DOWN))
                current.notional = float(current_notional.quantize(EIGHT_DP, rounding=ROUND_DOWN))
            else:
                intents_by_symbol[symbol] = intent

            used_notional += Decimal(str(intent.notional))

            if round_robin_index >= len(sorted_symbols) * 8:
                break

        intents = [intents_by_symbol[symbol] for symbol in sorted(intents_by_symbol)]
        if not intents:
            return []

        payload_hash = self._payload_hash(intents)
        if self.state_store.record_action(cycle_id, "sweep_plan", payload_hash) is None:
            logger.info(
                "Skipping duplicate sweep plan in dedupe window",
                extra={"extra": {"cycle_id": cycle_id, "payload_hash": payload_hash}},
            )
            return []

        return intents

    def _intent_for_budget(
        self,
        cycle_id: str,
        symbol: str,
        budget: Decimal,
        best_bids: dict[str, float],
        symbol_rules: dict[str, SymbolInfo],
    ) -> OrderIntent | None:
        bid = best_bids.get(symbol)
        if bid is None or bid <= 0:
            logger.warning(
                "Skipping symbol due to missing or non-positive bid",
                extra={"extra": {"symbol": symbol, "bid": bid}},
            )
            return None

        rule = symbol_rules.get(symbol)
        bid_dec = Decimal(str(bid))
        safe_price = bid_dec * (Decimal("1") - (self.offset_bps / Decimal("10000")))
        price = self._round_price(safe_price, rule.tick_size if rule else None)
        if price <= 0:
            return None

        step_size = rule.step_size if rule else None
        qty = self._round_qty(budget / price, step_size)
        if qty <= 0:
            return None

        notional = (qty * price).quantize(EIGHT_DP, rounding=ROUND_DOWN)
        if notional > budget:
            if step_size and step_size > 0:
                step = Decimal(str(step_size))
                qty = self._round_qty(max(Decimal("0"), qty - step), step_size)
            else:
                qty = self._round_qty(max(Decimal("0"), qty - EIGHT_DP), None)
            notional = (qty * price).quantize(EIGHT_DP, rounding=ROUND_DOWN)

        min_notional = (
            Decimal(str(rule.min_notional))
            if rule and rule.min_notional is not None
            else self.default_min_notional
        )
        if qty <= 0 or notional < min_notional:
            return None

        return OrderIntent(
            symbol=symbol,
            price=float(price),
            quantity=float(qty),
            notional=float(notional),
            cycle_id=cycle_id,
        )

    def _try_free_balance(self, balances: list[Balance]) -> Decimal:
        for balance in balances:
            if balance.asset.upper() == "TRY":
                return Decimal(str(balance.free))
        return Decimal("0")

    def _round_price(self, value: Decimal, tick_size: float | None) -> Decimal:
        if not tick_size or tick_size <= 0:
            return value.quantize(TWO_DP, rounding=ROUND_DOWN)

        tick = Decimal(str(tick_size))
        return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick

    def _round_qty(self, value: Decimal, step_size: float | None) -> Decimal:
        if not step_size or step_size <= 0:
            return value.quantize(EIGHT_DP, rounding=ROUND_DOWN)

        step = Decimal(str(step_size))
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    def _payload_hash(self, intents: list[OrderIntent]) -> str:
        normalized = [
            {
                "symbol": intent.symbol,
                "price": f"{intent.price:.8f}",
                "quantity": f"{intent.quantity:.8f}",
                "notional": f"{intent.notional:.8f}",
            }
            for intent in intents
        ]
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
