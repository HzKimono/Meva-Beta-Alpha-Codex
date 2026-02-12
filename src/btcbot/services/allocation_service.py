from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.allocation import (
    AllocationDecision,
    AllocationResult,
    ReasonCode,
    SizedAction,
)
from btcbot.domain.strategy_core import Intent, PositionSummary
from btcbot.domain.symbols import canonical_symbol

REASON_OK: ReasonCode = "ok"
REASON_NOT_IMPLEMENTED: ReasonCode = "not_implemented_in_pr3"
REASON_NO_MARK_PRICE: ReasonCode = "no_mark_price"
REASON_CASH_TARGET: ReasonCode = "cash_target"
REASON_MIN_NOTIONAL: ReasonCode = "min_notional"
REASON_MAX_INTENT_CAP: ReasonCode = "max_intent_cap"
REASON_CYCLE_CAP: ReasonCode = "cycle_notional_cap"
REASON_MAX_POSITION_CAP: ReasonCode = "max_position_exposure_cap"
REASON_NO_POSITION: ReasonCode = "no_position"
REASON_POSITION_VALUE_CAP: ReasonCode = "position_value_cap"


@dataclass(frozen=True)
class AllocationKnobs:
    """Allocation controls.

    Any cap field set to Decimal("0") is treated as disabled.
    """

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
            normalized_balances.get("TRY", Decimal("0")) - knobs.target_try_cash,
            Decimal("0"),
        )
        remaining_cycle_notional = knobs.max_total_notional_try_per_cycle

        for intent_index, intent in enumerate(intents):
            symbol = canonical_symbol(intent.symbol)
            requested_notional = intent.target_notional_try

            if intent.intent_type != "place":
                _append_decision(
                    decisions,
                    counters,
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason=REASON_NOT_IMPLEMENTED,
                        strategy_id=intent.strategy_id,
                        intent_index=intent_index,
                    ),
                )
                continue

            if requested_notional is None or requested_notional <= Decimal("0"):
                _append_decision(
                    decisions,
                    counters,
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason=REASON_MIN_NOTIONAL,
                        strategy_id=intent.strategy_id,
                        intent_index=intent_index,
                    ),
                )
                continue

            if intent.side == "buy":
                mark_price = normalized_prices.get(symbol)
                if mark_price is None or mark_price <= Decimal("0"):
                    _append_decision(
                        decisions,
                        counters,
                        AllocationDecision(
                            symbol=symbol,
                            side=intent.side,
                            intent_type=intent.intent_type,
                            requested_notional_try=requested_notional,
                            allocated_notional_try=None,
                            allocated_qty=None,
                            status="rejected",
                            reason=REASON_NO_MARK_PRICE,
                            strategy_id=intent.strategy_id,
                            intent_index=intent_index,
                        ),
                    )
                    continue

                position = normalized_positions.get(symbol)
                current_position_notional = Decimal("0")
                if position is not None and position.qty > Decimal("0"):
                    current_position_notional = position.qty * mark_price

                limits = {
                    REASON_CASH_TARGET: remaining_cash,
                    REASON_MAX_POSITION_CAP: _max_new_position_notional(
                        current_position_notional,
                        knobs.max_position_try_per_symbol,
                    ),
                    REASON_CYCLE_CAP: _apply_optional_cap(
                        requested_notional,
                        remaining_cycle_notional,
                        knobs.max_total_notional_try_per_cycle,
                    ),
                    REASON_MAX_INTENT_CAP: _apply_optional_cap(
                        requested_notional,
                        requested_notional,
                        knobs.max_intent_notional_try,
                    ),
                }

                allocated_notional = min(requested_notional, *limits.values())
                binding = _first_binding_reason(
                    requested_notional=requested_notional,
                    allocated_notional=allocated_notional,
                    limits=limits,
                    precedence=(
                        REASON_CASH_TARGET,
                        REASON_MAX_POSITION_CAP,
                        REASON_CYCLE_CAP,
                        REASON_MAX_INTENT_CAP,
                    ),
                )

                if allocated_notional <= Decimal("0"):
                    _append_decision(
                        decisions,
                        counters,
                        AllocationDecision(
                            symbol=symbol,
                            side=intent.side,
                            intent_type=intent.intent_type,
                            requested_notional_try=requested_notional,
                            allocated_notional_try=None,
                            allocated_qty=None,
                            status="rejected",
                            reason=binding,
                            strategy_id=intent.strategy_id,
                            intent_index=intent_index,
                        ),
                    )
                    continue

                if allocated_notional < knobs.min_order_notional_try:
                    reject_reason = (
                        REASON_CASH_TARGET if binding == REASON_CASH_TARGET else REASON_MIN_NOTIONAL
                    )
                    _append_decision(
                        decisions,
                        counters,
                        AllocationDecision(
                            symbol=symbol,
                            side=intent.side,
                            intent_type=intent.intent_type,
                            requested_notional_try=requested_notional,
                            allocated_notional_try=None,
                            allocated_qty=None,
                            status="rejected",
                            reason=reject_reason,
                            strategy_id=intent.strategy_id,
                            intent_index=intent_index,
                        ),
                    )
                    continue

                decision_reason = REASON_OK if allocated_notional == requested_notional else binding
                qty = allocated_notional / mark_price
                status = "accepted" if decision_reason == REASON_OK else "scaled"
                decision = AllocationDecision(
                    symbol=symbol,
                    side=intent.side,
                    intent_type=intent.intent_type,
                    requested_notional_try=requested_notional,
                    allocated_notional_try=allocated_notional,
                    allocated_qty=qty,
                    status=status,
                    reason=decision_reason,
                    strategy_id=intent.strategy_id,
                    intent_index=intent_index,
                )
                _append_decision(decisions, counters, decision)
                actions.append(
                    SizedAction(
                        symbol=symbol,
                        side=intent.side,
                        notional_try=allocated_notional,
                        qty=qty,
                        rationale=f"allocation:{decision.reason}",
                        strategy_id=intent.strategy_id,
                        intent_index=intent_index,
                    )
                )
                remaining_cash -= allocated_notional
                if knobs.max_total_notional_try_per_cycle > Decimal("0"):
                    remaining_cycle_notional -= allocated_notional
                continue

            position = normalized_positions.get(symbol)
            if position is None or position.qty <= Decimal("0"):
                _append_decision(
                    decisions,
                    counters,
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason=REASON_NO_POSITION,
                        strategy_id=intent.strategy_id,
                        intent_index=intent_index,
                    ),
                )
                continue

            mark_price = normalized_prices.get(symbol)
            if mark_price is None or mark_price <= Decimal("0"):
                _append_decision(
                    decisions,
                    counters,
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason=REASON_NO_MARK_PRICE,
                        strategy_id=intent.strategy_id,
                        intent_index=intent_index,
                    ),
                )
                continue

            position_value = position.qty * mark_price
            limits = {
                REASON_POSITION_VALUE_CAP: position_value,
                REASON_CYCLE_CAP: _apply_optional_cap(
                    requested_notional,
                    remaining_cycle_notional,
                    knobs.max_total_notional_try_per_cycle,
                ),
                REASON_MAX_INTENT_CAP: _apply_optional_cap(
                    requested_notional,
                    requested_notional,
                    knobs.max_intent_notional_try,
                ),
            }
            allocated_notional = min(requested_notional, *limits.values())
            binding = _first_binding_reason(
                requested_notional=requested_notional,
                allocated_notional=allocated_notional,
                limits=limits,
                precedence=(
                    REASON_POSITION_VALUE_CAP,
                    REASON_CYCLE_CAP,
                    REASON_MAX_INTENT_CAP,
                ),
            )

            if allocated_notional < knobs.min_order_notional_try:
                _append_decision(
                    decisions,
                    counters,
                    AllocationDecision(
                        symbol=symbol,
                        side=intent.side,
                        intent_type=intent.intent_type,
                        requested_notional_try=requested_notional,
                        allocated_notional_try=None,
                        allocated_qty=None,
                        status="rejected",
                        reason=REASON_MIN_NOTIONAL,
                        strategy_id=intent.strategy_id,
                        intent_index=intent_index,
                    ),
                )
                continue

            decision_reason = REASON_OK if allocated_notional == requested_notional else binding
            qty = allocated_notional / mark_price
            status = "accepted" if decision_reason == REASON_OK else "scaled"
            decision = AllocationDecision(
                symbol=symbol,
                side=intent.side,
                intent_type=intent.intent_type,
                requested_notional_try=requested_notional,
                allocated_notional_try=allocated_notional,
                allocated_qty=qty,
                status=status,
                reason=decision_reason,
                strategy_id=intent.strategy_id,
                intent_index=intent_index,
            )
            _append_decision(decisions, counters, decision)
            actions.append(
                SizedAction(
                    symbol=symbol,
                    side=intent.side,
                    notional_try=allocated_notional,
                    qty=qty,
                    rationale=f"allocation:{decision.reason}",
                    strategy_id=intent.strategy_id,
                    intent_index=intent_index,
                )
            )
            if knobs.max_total_notional_try_per_cycle > Decimal("0"):
                remaining_cycle_notional -= allocated_notional

        return AllocationResult(
            actions=tuple(actions), decisions=tuple(decisions), counters=counters
        )


def _apply_optional_cap(
    requested_notional: Decimal,
    cap_value: Decimal,
    configured_cap: Decimal,
) -> Decimal:
    if configured_cap <= Decimal("0"):
        return requested_notional
    return max(cap_value, Decimal("0"))


def _max_new_position_notional(
    current_position_notional: Decimal,
    max_position_try_per_symbol: Decimal,
) -> Decimal:
    if max_position_try_per_symbol <= Decimal("0"):
        return Decimal("Infinity")
    return max(max_position_try_per_symbol - current_position_notional, Decimal("0"))


def _first_binding_reason(
    *,
    requested_notional: Decimal,
    allocated_notional: Decimal,
    limits: Mapping[ReasonCode, Decimal],
    precedence: tuple[ReasonCode, ...],
) -> ReasonCode:
    if allocated_notional >= requested_notional:
        return REASON_OK
    for reason in precedence:
        if limits[reason] == allocated_notional:
            return reason
    return precedence[-1]


def _append_decision(
    decisions: list[AllocationDecision],
    counters: dict[str, int],
    decision: AllocationDecision,
) -> None:
    decisions.append(decision)
    _increment(counters, decision.status)
    _increment(counters, f"{decision.status}_{decision.reason}")


def _increment(counters: dict[str, int], key: str) -> None:
    counters[key] = counters.get(key, 0) + 1
