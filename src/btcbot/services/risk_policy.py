from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, Order, PnLSnapshot, Position


@dataclass(frozen=True)
class RiskDecision:
    action: LifecycleAction
    accepted: bool
    reason: str


class RiskPolicy:
    def __init__(
        self,
        *,
        max_open_orders: int,
        max_order_notional_try: Decimal | None = None,
        max_position_notional_try: Decimal,
        max_daily_loss_try: Decimal,
        max_drawdown_pct: Decimal,
        fee_bps_taker: Decimal,
        slippage_bps_buffer: Decimal,
        min_profit_bps: Decimal,
        replace_inflight_budget_per_symbol_try: Decimal | None = None,
        max_gross_exposure_try: Decimal | None = None,
    ) -> None:
        self.max_open_orders = max_open_orders
        self.max_order_notional_try = (
            max_order_notional_try
            if max_order_notional_try is not None
            else max_position_notional_try
        )
        self.max_position_notional_try = max_position_notional_try
        self.max_daily_loss_try = max_daily_loss_try
        self.max_drawdown_pct = max_drawdown_pct
        self.fee_bps_taker = fee_bps_taker
        self.slippage_bps_buffer = slippage_bps_buffer
        self.min_profit_bps = min_profit_bps
        self.replace_inflight_budget_per_symbol_try = (
            replace_inflight_budget_per_symbol_try
            if replace_inflight_budget_per_symbol_try is not None
            else max_position_notional_try
        )
        self.max_gross_exposure_try = (
            max_gross_exposure_try
            if max_gross_exposure_try is not None
            else max_position_notional_try
        )

    def filter_actions(
        self,
        actions: list[LifecycleAction],
        *,
        open_orders_count: int,
        current_position_notional_try: Decimal,
        pnl: PnLSnapshot,
        positions_by_symbol: dict[str, Position],
        open_orders_by_client_id: dict[str, Order] | None = None,
    ) -> tuple[list[LifecycleAction], list[RiskDecision]]:
        accepted: list[LifecycleAction] = []
        decisions: list[RiskDecision] = []

        if pnl.realized_today_try <= (self.max_daily_loss_try * Decimal("-1")):
            return [], [
                RiskDecision(action=a, accepted=False, reason="max_daily_loss") for a in actions
            ]
        if pnl.drawdown_pct >= self.max_drawdown_pct:
            return [], [
                RiskDecision(action=a, accepted=False, reason="max_drawdown") for a in actions
            ]

        projected_open_orders = open_orders_count
        projected_position_notional = current_position_notional_try
        min_required_bps = self.fee_bps_taker + self.slippage_bps_buffer + self.min_profit_bps
        open_orders_lookup = open_orders_by_client_id or {}

        for action in actions:
            if action.action_type == LifecycleActionType.CANCEL:
                projected_open_orders = max(0, projected_open_orders - 1)
                accepted.append(action)
                decisions.append(RiskDecision(action=action, accepted=True, reason="accepted"))
                continue

            if action.action_type != LifecycleActionType.SUBMIT:
                accepted.append(action)
                decisions.append(RiskDecision(action=action, accepted=True, reason="accepted"))
                continue

            projected_open_orders += 1
            if projected_open_orders > self.max_open_orders:
                projected_open_orders -= 1
                decisions.append(
                    RiskDecision(action=action, accepted=False, reason="max_open_orders")
                )
                continue

            action_notional = action.price * action.qty
            if action_notional > self.max_order_notional_try:
                projected_open_orders -= 1
                decisions.append(
                    RiskDecision(
                        action=action,
                        accepted=False,
                        reason="max_order_notional_try",
                    )
                )
                continue

            if action.side.lower() == "buy":
                is_replace_submit = (
                    action.reason == "replace_submit"
                    or action.replace_for_client_order_id is not None
                )
                if is_replace_submit:
                    old_notional = Decimal("0")
                    if action.replace_for_client_order_id:
                        old_order = open_orders_lookup.get(action.replace_for_client_order_id)
                        if old_order is not None:
                            old_notional = old_order.price * old_order.qty
                    new_notional = action_notional
                    symbol_budget = old_notional + new_notional
                    worst_case_global = projected_position_notional + old_notional + new_notional
                    if (
                        symbol_budget > self.replace_inflight_budget_per_symbol_try
                        or worst_case_global > self.max_gross_exposure_try
                    ):
                        projected_open_orders -= 1
                        decisions.append(
                            RiskDecision(
                                action=action,
                                accepted=False,
                                reason="replace_worst_case_exposure_blocked",
                            )
                        )
                        continue
                projected_position_notional += action_notional
                if projected_position_notional > self.max_position_notional_try:
                    projected_open_orders -= 1
                    projected_position_notional -= action_notional
                    decisions.append(
                        RiskDecision(
                            action=action,
                            accepted=False,
                            reason="max_position_notional_try",
                        )
                    )
                    continue
            else:
                projected_position_notional = max(
                    Decimal("0"), projected_position_notional - action_notional
                )
                position = positions_by_symbol.get(action.symbol)
                if position is not None and position.qty > 0:
                    required_price = position.avg_cost_try * (
                        Decimal("1") + (min_required_bps / Decimal("10000"))
                    )
                    if action.price < required_price:
                        projected_open_orders -= 1
                        decisions.append(
                            RiskDecision(
                                action=action, accepted=False, reason="min_profit_threshold"
                            )
                        )
                        continue

            accepted.append(action)
            decisions.append(RiskDecision(action=action, accepted=True, reason="accepted"))

        return accepted, decisions
