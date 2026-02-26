from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_engine import CycleRiskOutput
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType


@dataclass(frozen=True)
class RiskPolicyDecision:
    accepted: bool
    reason: str
    action: LifecycleAction


@dataclass(frozen=True)
class ActionPortfolioSnapshot:
    positions_by_symbol: dict[str, Decimal]


class RiskPolicyService:
    def filter_actions(
        self,
        *,
        actions: list[LifecycleAction],
        portfolio: ActionPortfolioSnapshot,
        cycle_risk: CycleRiskOutput,
    ) -> tuple[list[LifecycleAction], list[RiskPolicyDecision]]:
        accepted: list[LifecycleAction] = []
        decisions: list[RiskPolicyDecision] = []
        submit_count = 0

        for action in actions:
            if action.action_type == LifecycleActionType.CANCEL:
                if cycle_risk.allow_cancel:
                    accepted.append(action)
                    decisions.append(RiskPolicyDecision(True, "accepted", action))
                else:
                    decisions.append(RiskPolicyDecision(False, "cancel_blocked", action))
                continue

            if action.action_type != LifecycleActionType.SUBMIT:
                decisions.append(RiskPolicyDecision(False, "unsupported_action", action))
                continue

            if not cycle_risk.allow_submit:
                decisions.append(RiskPolicyDecision(False, "submit_blocked", action))
                continue

            submit_count += 1
            if submit_count > cycle_risk.max_orders_per_cycle:
                submit_count -= 1
                decisions.append(RiskPolicyDecision(False, "max_orders_per_cycle", action))
                continue

            notional = action.price * action.qty
            if notional > cycle_risk.max_order_notional_try:
                submit_count -= 1
                decisions.append(RiskPolicyDecision(False, "max_order_notional_try", action))
                continue

            symbol_pos = portfolio.positions_by_symbol.get(action.symbol, Decimal("0"))
            projected = symbol_pos + (action.qty if action.side.upper() == "BUY" else -action.qty)
            if (abs(projected) * action.price) > cycle_risk.max_symbol_exposure_try:
                submit_count -= 1
                decisions.append(RiskPolicyDecision(False, "max_symbol_exposure_try", action))
                continue

            if cycle_risk.mode == Mode.REDUCE_RISK_ONLY:
                if action.side.upper() == "SELL" and symbol_pos > 0:
                    accepted.append(action)
                    decisions.append(RiskPolicyDecision(True, "accepted", action))
                elif action.side.upper() == "BUY" and symbol_pos < 0:
                    accepted.append(action)
                    decisions.append(RiskPolicyDecision(True, "accepted", action))
                else:
                    submit_count -= 1
                    decisions.append(RiskPolicyDecision(False, "mode_reduce_risk_only", action))
                continue

            accepted.append(action)
            decisions.append(RiskPolicyDecision(True, "accepted", action))

        return accepted, decisions
