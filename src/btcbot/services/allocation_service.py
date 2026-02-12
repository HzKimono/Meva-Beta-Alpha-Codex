from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.allocation import AllocationDecision, AllocationResult, SizedAction
from btcbot.domain.strategy_core import Intent, PositionSummary
from btcbot.domain.symbols import canonical_symbol


@dataclass(frozen=True)
class AllocationKnobs:
    target_try_cash: Decimal = Decimal("300")
    min_order_notional_try: Decimal = Decimal("10")
    max_intent_notional_try: Decimal = Decimal("0")
    max_position_try_per_symbol: Decimal = Decimal("0")
    max_total_notional_try_per_cycle: Decimal = Decimal("0")


class AllocationService:
    @staticmethod
    def allocate(
        *,
        intents: list[Intent],
        balances: Mapping[str, Decimal],
        positions: Mapping[str, PositionSummary] | None,
        mark_prices: Mapping[str, Decimal],
        knobs: AllocationKnobs,
    ) -> AllocationResult:
        normalized_balances = {
            str(asset).upper(): amount for asset, amount in dict(balances).items()
        }
        normalized_positions = {
            canonical_symbol(symbol): position for symbol, position in dict(positions or {}).items()
        }
        normalized_prices = {
            canonical_symbol(symbol): price for symbol, price in dict(mark_prices).items()
        }

        decisions: list[AllocationDecision] = []
        actions: list[SizedAction] = []
        counters: dict[str, int] = {}

        remaining_cash = max(
            normalized_balances.get("TRY", Decimal("0")) - knobs.target_try_cash, Decimal("0")
        )
        remaining_cycle_notional = knobs.max_total_notional_try_per_cycle

        for intent in intents:
            symbol = canonical_symbol(intent.symbol)
            requested_notional = intent.target_notional_try

            if intent.intent_type != "place":
                decision = AllocationDecision(
                    symbol=symbol,
                    side=intent.side,
                    intent_type=intent.intent_type,
                    requested_notional_try=requested_notional,
                    allocated_notional_try=None,
                    allocated_qty=None,
                    status="rejected",
                    reason="not_implemented_in_pr3",
                    strategy_id=intent.strategy_id,
                )
                decisions.append(decision)
                _increment(counters, "rejected_not_implemented_in_pr3")
                continue

            if requested_notional is None or requested_notional <= Decimal("0"):
                decision = AllocationDecision(
                    symbol=symbol,
                    side=intent.side,
                    intent_type=intent.intent_type,
                    requested_notional_try=requested_notional,
                    allocated_notional_try=None,
                    allocated_qty=None,
                    status="rejected",
                    reason="invalid_notional",
                    strategy_id=intent.strategy_id,
                )
                decisions.append(decision)
                _increment(counters, "rejected_invalid_notional")
                continue

            mark_price = normalized_prices.get(symbol)
            if mark_price is None or mark_price <= Decimal("0"):
                decision = AllocationDecision(
                    symbol=symbol,
                    side=intent.side,
                    intent_type=intent.intent_type,
                    requested_notional_try=requested_notional,
                    allocated_notional_try=None,
                    allocated_qty=None,
                    status="rejected",
                    reason="no_mark_price",
                    strategy_id=intent.strategy_id,
                )
                decisions.append(decision)
                _increment(counters, "rejected_no_mark_price")
                continue

            max_intent_cap = knobs.max_intent_notional_try
            capped_requested = requested_notional
            if max_intent_cap > Decimal("0"):
                capped_requested = min(capped_requested, max_intent_cap)

            if intent.side == "buy":
                current_position_notional = Decimal("0")
                position = normalized_positions.get(symbol)
                if position is not None and position.qty > Decimal("0"):
                    current_position_notional = position.qty * mark_price

                allocated_notional = capped_requested

                if knobs.max_total_notional_try_per_cycle > Decimal("0"):
                    allocated_notional = min(
                        allocated_notional, max(remaining_cycle_notional, Decimal("0"))
                    )

                if knobs.max_position_try_per_symbol > Decimal("0"):
                    max_new_for_symbol = (
                        knobs.max_position_try_per_symbol - current_position_notional
                    )
                    allocated_notional = min(
                        allocated_notional, max(max_new_for_symbol, Decimal("0"))
                    )

                allocated_notional = min(allocated_notional, remaining_cash)

                if allocated_notional <= Decimal("0"):
                    reason = "cash_target"
                    if (
                        knobs.max_position_try_per_symbol > Decimal("0")
                        and current_position_notional >= knobs.max_position_try_per_symbol
                    ):
                        reason = "max_exposure"
                    elif knobs.max_total_notional_try_per_cycle > Decimal(
                        "0"
                    ) and remaining_cycle_notional <= Decimal("0"):
                        reason = "cycle_notional_cap"
                    decision = AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason=reason,
                        strategy_id=intent.strategy_id,
                    )
                    decisions.append(decision)
                    _increment(counters, f"rejected_{reason}")
                    continue

                if allocated_notional < knobs.min_order_notional_try:
                    reason = "min_notional"
                    if remaining_cash < knobs.min_order_notional_try:
                        reason = "cash_target"
                    decision = AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason=reason,
                        strategy_id=intent.strategy_id,
                    )
                    decisions.append(decision)
                    _increment(counters, f"rejected_{reason}")
                    continue

                status = "accepted" if allocated_notional == requested_notional else "scaled"
                qty = allocated_notional / mark_price
                decisions.append(
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=allocated_notional,
                        allocated_qty=qty,
                        status=status,
                        reason="ok",
                        strategy_id=intent.strategy_id,
                    )
                )
                actions.append(
                    SizedAction(
                        symbol=symbol,
                        side=intent.side,
                        notional_try=allocated_notional,
                        qty=qty,
                        rationale=intent.rationale,
                        strategy_id=intent.strategy_id,
                    )
                )
                _increment(counters, status)
                remaining_cash -= allocated_notional
                if knobs.max_total_notional_try_per_cycle > Decimal("0"):
                    remaining_cycle_notional -= allocated_notional
                continue

            position = normalized_positions.get(symbol)
            if position is None or position.qty <= Decimal("0"):
                decisions.append(
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason="no_position",
                        strategy_id=intent.strategy_id,
                    )
                )
                _increment(counters, "rejected_no_position")
                continue

            position_value = position.qty * mark_price
            allocated_notional = min(capped_requested, position_value)

            if knobs.max_total_notional_try_per_cycle > Decimal("0"):
                allocated_notional = min(
                    allocated_notional, max(remaining_cycle_notional, Decimal("0"))
                )

            if allocated_notional < knobs.min_order_notional_try:
                decisions.append(
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason="min_notional",
                        strategy_id=intent.strategy_id,
                    )
                )
                _increment(counters, "rejected_min_notional")
                continue

            qty = allocated_notional / mark_price
            status = "accepted" if allocated_notional == requested_notional else "scaled"
            decisions.append(
                AllocationDecision(
                    symbol=symbol,
                    side=intent.side,
                    intent_type=intent.intent_type,
                    requested_notional_try=requested_notional,
                    allocated_notional_try=allocated_notional,
                    allocated_qty=qty,
                    status=status,
                    reason="ok",
                    strategy_id=intent.strategy_id,
                )
            )
            actions.append(
                SizedAction(
                    symbol=symbol,
                    side=intent.side,
                    notional_try=allocated_notional,
                    qty=qty,
                    rationale=intent.rationale,
                    strategy_id=intent.strategy_id,
                )
            )
            _increment(counters, status)
            if knobs.max_total_notional_try_per_cycle > Decimal("0"):
                remaining_cycle_notional -= allocated_notional

        return AllocationResult(actions=actions, decisions=decisions, counters=counters)


def _increment(counters: dict[str, int], key: str) -> None:
    counters[key] = counters.get(key, 0) + 1
