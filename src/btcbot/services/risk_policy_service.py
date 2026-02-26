from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_engine import CycleRiskOutput
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType

REASON_RISK_SUBMIT_DISABLED = "RISK_SUBMIT_DISABLED"
REASON_RISK_CANCEL_DISABLED = "RISK_CANCEL_DISABLED"
REASON_RISK_CAP_MAX_ORDERS = "RISK_CAP_MAX_ORDERS"
REASON_RISK_CAP_MAX_NOTIONAL = "RISK_CAP_MAX_NOTIONAL"
REASON_RISK_CAP_MAX_EXPOSURE = "RISK_CAP_MAX_EXPOSURE"
REASON_RISK_REDUCE_ONLY_BLOCKED = "RISK_REDUCE_ONLY_BLOCKED"
REASON_ACCEPTED = "accepted"


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
            symbol = action.symbol
            symbol_pos = portfolio.positions_by_symbol.get(symbol, Decimal("0"))
            if action.action_type == LifecycleActionType.CANCEL:
                if not cycle_risk.allow_cancel:
                    decisions.append(
                        RiskPolicyDecision(False, REASON_RISK_CANCEL_DISABLED, action)
                    )
                    continue
                accepted.append(action)
                decisions.append(RiskPolicyDecision(True, REASON_ACCEPTED, action))
                continue

            if action.action_type != LifecycleActionType.SUBMIT:
                decisions.append(RiskPolicyDecision(True, REASON_ACCEPTED, action))
                accepted.append(action)
                continue

            if not cycle_risk.allow_submit:
                decisions.append(RiskPolicyDecision(False, REASON_RISK_SUBMIT_DISABLED, action))
                continue

            if cycle_risk.mode == Mode.REDUCE_RISK_ONLY:
                is_reduce = (action.side.upper() == "SELL" and symbol_pos > 0) or (
                    action.side.upper() == "BUY" and symbol_pos < 0
                )
                if not is_reduce:
                    decisions.append(
                        RiskPolicyDecision(False, REASON_RISK_REDUCE_ONLY_BLOCKED, action)
                    )
                    continue

            submit_count += 1
            if submit_count > cycle_risk.max_orders_per_cycle:
                submit_count -= 1
                decisions.append(RiskPolicyDecision(False, REASON_RISK_CAP_MAX_ORDERS, action))
                continue

            notional = action.price * action.qty
            if notional > cycle_risk.max_order_notional_try:
                submit_count -= 1
                decisions.append(
                    RiskPolicyDecision(False, REASON_RISK_CAP_MAX_NOTIONAL, action)
                )
                continue

            projected = symbol_pos + (action.qty if action.side.upper() == "BUY" else -action.qty)
            if (abs(projected) * action.price) > cycle_risk.max_symbol_exposure_try:
                submit_count -= 1
                decisions.append(
                    RiskPolicyDecision(False, REASON_RISK_CAP_MAX_EXPOSURE, action)
                )
                continue

            accepted.append(action)
            decisions.append(RiskPolicyDecision(True, REASON_ACCEPTED, action))

        return accepted, decisions
