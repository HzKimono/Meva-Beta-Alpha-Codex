from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from btcbot.config import Settings
from btcbot.domain.models import Balance, normalize_symbol
from btcbot.domain.portfolio_policy_models import (
    PortfolioPlan,
    PortfolioSnapshot,
    PositionSnapshot,
    RebalanceAction,
    TargetAllocation,
)
from btcbot.domain.risk_budget import Mode


@dataclass(frozen=True)
class _RawAction:
    symbol: str
    side: Literal["BUY", "SELL"]
    requested_notional_try: Decimal
    reason: str


def split_symbol(symbol: str, quote_ccy: str) -> tuple[str, str]:
    """Deterministically split symbol into (base, quote) with graceful fallback."""

    cleaned = str(symbol).strip().upper().replace(" ", "")
    quote = str(quote_ccy).strip().upper()
    if not cleaned:
        return cleaned, ""

    for separator in ("_", "-"):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            if left and right:
                return left, right
            return cleaned, ""

    canonical = normalize_symbol(cleaned)
    if quote and canonical.endswith(quote) and len(canonical) > len(quote):
        return canonical[: -len(quote)], quote

    return canonical, ""


class PortfolioPolicyService:
    _QTY_PRECISION = Decimal("0.00000001")

    def build_plan(
        self,
        *,
        universe: list[str],
        mark_prices_try: dict[str, Decimal],
        balances: list[Balance],
        settings: Settings,
        now_utc: datetime,
        final_mode: Mode = Mode.NORMAL,
    ) -> PortfolioPlan:
        normalized_universe = sorted({normalize_symbol(symbol) for symbol in universe})
        price_map = {
            normalize_symbol(symbol): Decimal(str(price))
            for symbol, price in mark_prices_try.items()
            if Decimal(str(price)) > 0
        }
        filtered_universe = [symbol for symbol in normalized_universe if symbol in price_map]

        quote_ccy = str(settings.stage7_universe_quote_ccy).upper()
        max_position_notional_try = Decimal(str(settings.max_position_notional_try))
        turnover_cap_try = Decimal(str(settings.notional_cap_try_per_cycle))
        min_order_notional_try = Decimal(str(settings.min_order_notional_try))
        cash_target_cfg = Decimal(str(settings.try_cash_target))
        cash_max_cfg = Decimal(str(settings.try_cash_max))
        cash_target_try = min(cash_target_cfg, cash_max_cfg)

        snapshot = self._build_snapshot(
            universe=filtered_universe,
            mark_prices_try=price_map,
            balances=balances,
            quote_ccy=quote_ccy,
        )

        investable_equity = max(Decimal("0"), snapshot.equity_try - cash_target_try)

        allocations, notes = self._build_allocations(
            universe=filtered_universe,
            investable_equity=investable_equity,
            max_position_notional_try=max_position_notional_try,
        )

        raw_actions = self._build_raw_actions(
            allocations=allocations,
            positions=snapshot.positions,
        )

        constrained_actions, dropped_notes = self._apply_constraints(
            raw_actions=raw_actions,
            prices=price_map,
            min_order_notional_try=min_order_notional_try,
            turnover_cap_try=turnover_cap_try,
            max_orders_per_cycle=settings.max_orders_per_cycle,
        )

        mode_notes: list[str] = []
        if final_mode == Mode.OBSERVE_ONLY:
            if constrained_actions:
                mode_notes.append(f"observe_only={len(constrained_actions)}")
            constrained_actions = []
        elif final_mode == Mode.REDUCE_RISK_ONLY:
            before_count = len(constrained_actions)
            constrained_actions = [
                action for action in constrained_actions if action.side == "SELL"
            ]
            dropped = before_count - len(constrained_actions)
            if dropped > 0:
                mode_notes.append(f"mode_reduce_risk_only={dropped}")

        constraints_summary = {
            "try_cash_target": str(cash_target_cfg),
            "try_cash_max": str(cash_max_cfg),
            "cash_target_applied": str(cash_target_try),
            "max_position_notional_try": str(max_position_notional_try),
            "notional_cap_try_per_cycle": str(turnover_cap_try),
            "max_orders_per_cycle": str(settings.max_orders_per_cycle),
            "min_order_notional_try": str(min_order_notional_try),
            "final_mode": final_mode.value,
            "snapshot": str(snapshot.to_dict()),
        }

        return PortfolioPlan(
            timestamp=now_utc,
            universe=filtered_universe,
            cash_target_try=cash_target_try,
            allocations=allocations,
            actions=constrained_actions,
            constraints_summary=constraints_summary,
            notes=notes + dropped_notes + mode_notes,
        )

    def _build_snapshot(
        self,
        *,
        universe: list[str],
        mark_prices_try: dict[str, Decimal],
        balances: list[Balance],
        quote_ccy: str,
    ) -> PortfolioSnapshot:
        cash_try = Decimal("0")
        balance_by_asset = {
            str(balance.asset).upper(): Decimal(str(balance.free)) for balance in balances
        }
        cash_try = balance_by_asset.get(quote_ccy, Decimal("0"))

        positions: list[PositionSnapshot] = []
        total_notional = Decimal("0")
        for symbol in universe:
            base_asset, parsed_quote = split_symbol(symbol, quote_ccy)
            if parsed_quote != quote_ccy:
                qty = Decimal("0")
            else:
                qty = balance_by_asset.get(base_asset, Decimal("0"))
            mark = mark_prices_try[symbol]
            notional = qty * mark
            total_notional += notional
            positions.append(
                PositionSnapshot(
                    symbol=symbol,
                    qty=qty,
                    mark_price_try=mark,
                    notional_try=notional,
                )
            )

        return PortfolioSnapshot(
            cash_try=cash_try,
            positions=sorted(positions, key=lambda position: position.symbol),
            equity_try=cash_try + total_notional,
        )

    def _build_allocations(
        self,
        *,
        universe: list[str],
        investable_equity: Decimal,
        max_position_notional_try: Decimal,
    ) -> tuple[list[TargetAllocation], list[str]]:
        if not universe or investable_equity <= 0:
            return [], ["no investable equity or empty universe"]

        equal_weight = Decimal("1") / Decimal(len(universe))
        allocations: list[TargetAllocation] = []
        allocated_weight = Decimal("0")
        capped_count = 0

        for symbol in universe:
            equal_target = investable_equity * equal_weight
            target_notional = min(equal_target, max_position_notional_try)
            if equal_target > 0 and target_notional < equal_target:
                capped_count += 1
            weight = target_notional / investable_equity if investable_equity > 0 else Decimal("0")
            allocated_weight += weight
            allocations.append(
                TargetAllocation(
                    symbol=symbol,
                    weight=weight,
                    target_notional_try=target_notional,
                )
            )

        leftover = Decimal("1") - allocated_weight
        if leftover < 0:
            leftover = Decimal("0")
        notes = [
            f"leftover_weight_to_cash={leftover}",
            f"per_symbol_cap_hits={capped_count}",
        ]
        return allocations, notes

    def _build_raw_actions(
        self,
        *,
        allocations: list[TargetAllocation],
        positions: list[PositionSnapshot],
    ) -> list[_RawAction]:
        current_by_symbol = {position.symbol: position for position in positions}
        sells: list[_RawAction] = []
        buys: list[_RawAction] = []

        for allocation in allocations:
            current_notional = current_by_symbol.get(
                allocation.symbol,
                PositionSnapshot(
                    symbol=allocation.symbol,
                    qty=Decimal("0"),
                    mark_price_try=Decimal("1"),
                    notional_try=Decimal("0"),
                ),
            ).notional_try
            delta = allocation.target_notional_try - current_notional
            if delta == 0:
                continue
            if delta < 0:
                sells.append(
                    _RawAction(
                        symbol=allocation.symbol,
                        side="SELL",
                        requested_notional_try=abs(delta),
                        reason=(
                            "overweight: "
                            f"current={current_notional} target={allocation.target_notional_try}"
                        ),
                    )
                )
            else:
                buys.append(
                    _RawAction(
                        symbol=allocation.symbol,
                        side="BUY",
                        requested_notional_try=delta,
                        reason=(
                            "underweight: "
                            f"current={current_notional} target={allocation.target_notional_try}"
                        ),
                    )
                )

        return sorted(sells, key=lambda action: action.symbol) + sorted(
            buys, key=lambda action: action.symbol
        )

    def _apply_constraints(
        self,
        *,
        raw_actions: list[_RawAction],
        prices: dict[str, Decimal],
        min_order_notional_try: Decimal,
        turnover_cap_try: Decimal,
        max_orders_per_cycle: int,
    ) -> tuple[list[RebalanceAction], list[str]]:
        dropped_min = 0
        filtered: list[_RawAction] = []
        for action in raw_actions:
            if action.requested_notional_try < min_order_notional_try:
                dropped_min += 1
                continue
            filtered.append(action)

        sorted_actions = sorted(
            filtered,
            key=lambda action: (Decimal("0") - action.requested_notional_try, action.symbol),
        )

        constrained: list[RebalanceAction] = []
        remaining_turnover = turnover_cap_try
        dropped_turnover = 0

        for action in sorted_actions:
            if remaining_turnover <= 0:
                dropped_turnover += 1
                continue
            price = prices.get(action.symbol)
            if price is None or price <= 0:
                continue
            applied_notional = min(action.requested_notional_try, remaining_turnover)
            if applied_notional < min_order_notional_try:
                dropped_turnover += 1
                continue
            remaining_turnover -= applied_notional
            qty = (applied_notional / price).quantize(self._QTY_PRECISION, rounding=ROUND_DOWN)
            if qty <= 0:
                dropped_turnover += 1
                continue
            constrained.append(
                RebalanceAction(
                    symbol=action.symbol,
                    side=action.side,
                    target_notional_try=applied_notional,
                    est_qty=qty,
                    reason=f"{action.reason}; turnover_cap_remaining={remaining_turnover}",
                )
            )

        constrained = sorted(
            constrained,
            key=lambda action: (
                Decimal("0") - action.target_notional_try.copy_abs(),
                action.symbol,
            ),
        )
        kept = constrained[: max(0, max_orders_per_cycle)]
        dropped_max_orders = max(0, len(constrained) - len(kept))

        notes: list[str] = []
        if dropped_min > 0:
            notes.append(f"min_notional={dropped_min}")
        if dropped_turnover > 0:
            notes.append(f"turnover_cap={dropped_turnover}")
        if dropped_max_orders > 0:
            notes.append(f"max_orders={dropped_max_orders}")

        sells = sorted((action for action in kept if action.side == "SELL"), key=lambda a: a.symbol)
        buys = sorted((action for action in kept if action.side == "BUY"), key=lambda a: a.symbol)
        return sells + buys, notes
