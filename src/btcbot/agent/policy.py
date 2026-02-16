from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError

from btcbot.agent.contracts import (
    AgentContext,
    AgentDecision,
    DecisionAction,
    DecisionRationale,
    LlmDecisionEnvelope,
)

logger = logging.getLogger(__name__)


class AgentPolicy(Protocol):
    def evaluate(self, context: AgentContext) -> AgentDecision:
        ...


class LlmClient(Protocol):
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        ...


class LlmPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptBuildResult:
    prompt: str
    trimmed: bool


@dataclass(frozen=True)
class PromptBuilder:
    max_chars: int = 3500

    def build(self, context: AgentContext) -> PromptBuildResult:
        compact = {
            "cycle_id": context.cycle_id,
            "generated_at": context.generated_at.isoformat(),
            "market_snapshot": {k: str(v) for k, v in context.market_snapshot.items()},
            "market_spreads_bps": {k: str(v) for k, v in context.market_spreads_bps.items()},
            "portfolio": {k: str(v) for k, v in context.portfolio.items()},
            "open_orders": context.open_orders[:10],
            "risk_state": {k: str(v) for k, v in context.risk_state.items()},
            "recent_events": context.recent_events[:20],
            "is_live_mode": context.is_live_mode,
        }
        body = json.dumps(compact, separators=(",", ":"), sort_keys=True)
        trimmed = False
        if len(body) > self.max_chars:
            trimmed = True
            body = body[: self.max_chars]
        prompt = (
            "You are a trading policy assistant. "
            "Return STRICT JSON only matching schema keys: "
            "action, propose_intents, adjust_risk, observe_only, rationale. "
            "No markdown.\n"
            f"Context:{body}"
        )
        return PromptBuildResult(prompt=prompt, trimmed=trimmed)


@dataclass(frozen=True)
class RuleBasedPolicy:
    def evaluate(self, context: AgentContext) -> AgentDecision:
        reasons: list[str] = []
        constraints: list[str] = []

        stale_data = bool(context.risk_state.get("stale_data", False))
        kill_switch = bool(context.risk_state.get("kill_switch", False))
        safe_mode = bool(context.risk_state.get("safe_mode", False))

        if kill_switch:
            reasons.append("Kill switch active")
            constraints.append("kill_switch")
            return AgentDecision(
                action=DecisionAction.OBSERVE_ONLY,
                observe_only=True,
                rationale=DecisionRationale(
                    reasons=reasons,
                    confidence=1.0,
                    constraints_hit=constraints,
                    citations=["risk_state.kill_switch"],
                ),
            )

        if stale_data or safe_mode:
            if stale_data:
                reasons.append("Market data stale")
                constraints.append("stale_data")
            if safe_mode:
                reasons.append("Safe mode active")
                constraints.append("safe_mode")
            return AgentDecision(
                action=DecisionAction.OBSERVE_ONLY,
                observe_only=True,
                rationale=DecisionRationale(
                    reasons=reasons,
                    confidence=0.95,
                    constraints_hit=constraints,
                    citations=["risk_state"],
                ),
            )

        reasons.append("No policy override; keep upstream planning intents")
        return AgentDecision(
            action=DecisionAction.NO_OP,
            rationale=DecisionRationale(
                reasons=reasons,
                confidence=0.8,
                constraints_hit=[],
                citations=["planning_kernel"],
            ),
        )


@dataclass(frozen=True)
class LlmPolicy:
    client: LlmClient
    prompt_builder: PromptBuilder
    timeout_seconds: float = 3.0

    def evaluate(self, context: AgentContext) -> AgentDecision:
        prompt_result = self.prompt_builder.build(context)
        try:
            response = self.client.complete(prompt_result.prompt, timeout_seconds=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            raise LlmPolicyError(f"llm request failed: {type(exc).__name__}") from exc

        try:
            parsed = LlmDecisionEnvelope.model_validate_json(response)
        except ValidationError as exc:
            raise LlmPolicyError("llm response failed schema validation") from exc

        decision = AgentDecision(**parsed.model_dump())
        logger.debug(
            "agent_llm_decision",
            extra={
                "extra": {
                    "cycle_id": context.cycle_id,
                    "prompt_trimmed": prompt_result.trimmed,
                    "action": decision.action.value,
                    "generated_at": datetime.now(UTC).isoformat(),
                }
            },
        )
        return decision


@dataclass(frozen=True)
class FallbackPolicy:
    primary: AgentPolicy
    fallback: AgentPolicy

    def evaluate(self, context: AgentContext) -> AgentDecision:
        try:
            return self.primary.evaluate(context)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent_policy_primary_failed",
                extra={
                    "extra": {
                        "cycle_id": context.cycle_id,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            return self.fallback.evaluate(context)
