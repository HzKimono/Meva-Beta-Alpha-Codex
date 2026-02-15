from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.allocation import AllocationDecision, AllocationResult, ReasonCode, SizedAction
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
    # Definitions:
    # - cash_try: available TRY from balances
    # - target_try_cash: reserve floor that should remain after estimated fees
    # - try_cash_max: optional guardrail for maximum TRY deployable in this cycle
    # - investable_total_try: max(0, cash_try - target_try_cash)
    # - investable_this_cycle_try: policy-constrained portion of investable_total_try
    # - deploy_budget_try: notional budget that can be spent while keeping fee buffer
    target_try_cash: Decimal = Decimal("300")
    try_cash_max: Decimal = Decimal("0")
    min_order_notional_try: Decimal = Decimal("10")
    fee_buffer_bps: Decimal = Decimal("0")
    fee_buffer_ratio: Decimal = Decimal("0")
    max_intent_notional_try: Decimal = Decimal("0")
    max_position_try_per_symbol: Decimal = Decimal("0")
    max_total_notional_try_per_cycle: Decimal = Decimal("0")
    investable_usage_mode: str = "use_all"
    investable_usage_fraction: Decimal = Decimal("1")
    max_try_per_cycle: Decimal = Decimal("0")


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

        cash_try = normalized_balances.get("TRY", Decimal("0"))
        investable_total_try = max(cash_try - knobs.target_try_cash, Decimal("0"))
        investable_this_cycle_try, usage_reason = _resolve_investable_budget(
            investable_total_try, knobs
        )
        fee_buffer_ratio = _resolve_fee_buffer_ratio(knobs)
        fee_multiplier = Decimal("1") + fee_buffer_ratio
        deploy_budget_try = (
            investable_this_cycle_try / fee_multiplier
            if fee_multiplier > 0
            else investable_this_cycle_try
        )

        remaining_deploy_budget_try = deploy_budget_try
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
                    REASON_CASH_TARGET: remaining_deploy_budget_try,
                    REASON_MAX_POSITION_CAP: _max_new_position_notional(
                        current_position_notional, knobs.max_position_try_per_symbol
                    ),
                    REASON_CYCLE_CAP: _apply_optional_cap(
                        requested_notional,
                        remaining_cycle_notional,
                        knobs.max_total_notional_try_per_cycle,
                    ),
                }
                if knobs.max_intent_notional_try > Decimal("0"):
                    limits[REASON_MAX_INTENT_CAP] = knobs.max_intent_notional_try
                precedence = [REASON_CASH_TARGET, REASON_MAX_POSITION_CAP, REASON_CYCLE_CAP]
                if REASON_MAX_INTENT_CAP in limits:
                    precedence.append(REASON_MAX_INTENT_CAP)
            else:
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

                limits = {
                    REASON_POSITION_VALUE_CAP: position.qty * mark_price,
                    REASON_CYCLE_CAP: _apply_optional_cap(
                        requested_notional,
                        remaining_cycle_notional,
                        knobs.max_total_notional_try_per_cycle,
                    ),
                }
                if knobs.max_intent_notional_try > Decimal("0"):
                    limits[REASON_MAX_INTENT_CAP] = knobs.max_intent_notional_try
                precedence = [REASON_POSITION_VALUE_CAP, REASON_CYCLE_CAP]
                if REASON_MAX_INTENT_CAP in limits:
                    precedence.append(REASON_MAX_INTENT_CAP)

            allocated_notional = min(requested_notional, *limits.values())
            binding = _first_binding_reason(
                requested_notional=requested_notional,
                allocated_notional=allocated_notional,
                limits=limits,
                precedence=tuple(precedence),
            )

            diagnostics = {
                "computed_notional_try": str(allocated_notional),
                "required_min_notional_try": str(knobs.min_order_notional_try),
                "computed_qty": str(allocated_notional / mark_price),
                "adjusted_qty": None,
                "step_size": None,
                "price_used": str(mark_price),
                "cap_limits": {k: str(v) for k, v in limits.items()},
                "blocking_cap": None,
                "fee_buffer_ratio": str(fee_buffer_ratio),
            }

            if allocated_notional < knobs.min_order_notional_try:
                min_required = knobs.min_order_notional_try
                can_raise = all(
                    limits[key] >= min_required
                    for key in precedence
                    if key != REASON_MAX_INTENT_CAP
                )
                if can_raise:
                    allocated_notional = min_required
                    diagnostics["adjusted_qty"] = str(allocated_notional / mark_price)
                else:
                    blocking_cap = next(
                        (key for key in precedence if limits[key] < min_required), binding
                    )
                    diagnostics["blocking_cap"] = blocking_cap
                    reject_reason = (
                        REASON_CASH_TARGET
                        if blocking_cap == REASON_CASH_TARGET
                        else REASON_MIN_NOTIONAL
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
                            diagnostics=diagnostics,
                        ),
                    )
                    continue

            decision_reason = REASON_OK if allocated_notional == requested_notional else binding
            qty = allocated_notional / mark_price
            status = "accepted" if decision_reason == REASON_OK else "scaled"
            _append_decision(
                decisions,
                counters,
                AllocationDecision(
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
                    diagnostics=diagnostics,
                ),
            )
            actions.append(
                SizedAction(
                    symbol=symbol,
                    side=intent.side,
                    notional_try=allocated_notional,
                    qty=qty,
                    rationale=f"allocation:{decision_reason}",
                    strategy_id=intent.strategy_id,
                    intent_index=intent_index,
                )
            )
            if intent.side == "buy":
                remaining_deploy_budget_try -= allocated_notional
            if knobs.max_total_notional_try_per_cycle > Decimal("0"):
                remaining_cycle_notional -= allocated_notional

        planned_total_try = sum(action.notional_try for action in actions if action.side == "buy")
        unused_budget_try = max(Decimal("0"), deploy_budget_try - planned_total_try)

        return AllocationResult(
            actions=tuple(actions),
            decisions=tuple(decisions),
            counters=counters,
            cash_try=cash_try,
            try_cash_target=knobs.target_try_cash,
            investable_total_try=investable_total_try,
            investable_this_cycle_try=investable_this_cycle_try,
            deploy_budget_try=deploy_budget_try,
            planned_total_try=planned_total_try,
            unused_budget_try=unused_budget_try,
            investable_usage_reason=usage_reason,
        )


def _resolve_investable_budget(
    investable_total_try: Decimal, knobs: AllocationKnobs
) -> tuple[Decimal, str]:
    investable_this_cycle_try = investable_total_try
    usage_parts: list[str] = []

    mode = knobs.investable_usage_mode.strip().lower()
    if mode == "fraction":
        fraction = min(max(knobs.investable_usage_fraction, Decimal("0")), Decimal("1"))
        investable_this_cycle_try = investable_this_cycle_try * fraction
        usage_parts.append(f"fraction:{fraction}")
    elif mode == "cap":
        cap = max(knobs.max_try_per_cycle, Decimal("0"))
        investable_this_cycle_try = min(investable_this_cycle_try, cap)
        usage_parts.append(f"cap:{cap}")
    else:
        usage_parts.append("use_all")

    if knobs.try_cash_max > Decimal("0"):
        try_cash_max_investable = max(knobs.try_cash_max - knobs.target_try_cash, Decimal("0"))
        capped = min(investable_this_cycle_try, try_cash_max_investable)
        if capped != investable_this_cycle_try:
            usage_parts.append(f"try_cash_max:{knobs.try_cash_max}")
        investable_this_cycle_try = capped

    return investable_this_cycle_try, ",".join(usage_parts)


def _resolve_fee_buffer_ratio(knobs: AllocationKnobs) -> Decimal:
    if knobs.fee_buffer_ratio > Decimal("0"):
        return max(knobs.fee_buffer_ratio, Decimal("0"))
    return max(knobs.fee_buffer_bps, Decimal("0")) / Decimal("10000")


def _apply_optional_cap(
    requested_notional: Decimal, cap_value: Decimal, configured_cap: Decimal
) -> Decimal:
    if configured_cap <= Decimal("0"):
        return requested_notional
    return max(cap_value, Decimal("0"))


def _max_new_position_notional(
    current_position_notional: Decimal, max_position_try_per_symbol: Decimal
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
    decisions: list[AllocationDecision], counters: dict[str, int], decision: AllocationDecision
) -> None:
    decisions.append(decision)
    _increment(counters, decision.status)
    _increment(counters, f"{decision.status}_{decision.reason}")


def _increment(counters: dict[str, int], key: str) -> None:
    counters[key] = counters.get(key, 0) + 1
