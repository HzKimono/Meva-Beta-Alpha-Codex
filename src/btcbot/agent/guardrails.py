from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from btcbot.agent.contracts import AgentContext, AgentDecision, DecisionAction, SafeDecision


@dataclass(frozen=True)
class SafetyGuard:
    max_exposure_try: Decimal
    max_order_notional_try: Decimal
    max_drawdown_pct: Decimal
    min_notional_try: Decimal
    max_spread_bps: Decimal
    symbol_allowlist: set[str]
    cooldown_seconds: int
    stale_data_seconds: int
    kill_switch: bool
    safe_mode: bool
    observe_only_override: bool

    def apply(self, context: AgentContext, decision: AgentDecision) -> SafeDecision:
        blocked: list[str] = []
        dropped: list[str] = []

        if self.kill_switch:
            blocked.append("kill_switch")
        if self.safe_mode:
            blocked.append("safe_mode")
        if self.observe_only_override:
            blocked.append("observe_only_override")

        drawdown = Decimal(str(context.risk_state.get("drawdown_pct", "0")))
        if drawdown >= self.max_drawdown_pct:
            blocked.append("max_drawdown")

        age_seconds = Decimal(str(context.risk_state.get("market_data_age_seconds", "0")))
        if age_seconds >= Decimal(self.stale_data_seconds):
            blocked.append("stale_data_inhibit")

        cooldown_until_raw = context.risk_state.get("cooldown_until")
        if isinstance(cooldown_until_raw, str) and cooldown_until_raw:
            cooldown_until = datetime.fromisoformat(cooldown_until_raw)
            if context.generated_at < cooldown_until:
                blocked.append("cooldown")

        exposure = Decimal(str(context.risk_state.get("gross_exposure_try", "0")))
        if exposure >= self.max_exposure_try:
            blocked.append("max_exposure")

        if blocked:
            observe_decision = decision.model_copy(
                update={
                    "action": DecisionAction.OBSERVE_ONLY,
                    "observe_only": True,
                    "propose_intents": [],
                }
            )
            return SafeDecision(
                decision=observe_decision,
                blocked_reasons=sorted(set(blocked)),
                observe_only_override=True,
                diff={"action": [decision.action.value, DecisionAction.OBSERVE_ONLY.value]},
            )

        safe_intents = []
        for intent in decision.propose_intents:
            if intent.symbol not in self.symbol_allowlist:
                dropped.append(intent.symbol)
                continue
            if intent.notional_try < self.min_notional_try:
                dropped.append(intent.symbol)
                continue
            if intent.notional_try > self.max_order_notional_try:
                dropped.append(intent.symbol)
                continue
            spread = context.market_spreads_bps.get(intent.symbol, Decimal("0"))
            if spread > self.max_spread_bps:
                dropped.append(intent.symbol)
                continue
            safe_intents.append(intent)

        updated = decision.model_copy(update={"propose_intents": safe_intents})
        diff = {
            "dropped_intents": len(decision.propose_intents) - len(safe_intents),
            "kept_intents": len(safe_intents),
        }
        return SafeDecision(
            decision=updated,
            dropped_symbols=sorted(set(dropped)),
            observe_only_override=False,
            diff=diff,
        )
