from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
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
    payload_json: str
    trimmed: bool


@dataclass(frozen=True)
class PromptBuilder:
    max_chars: int = 3500
    max_symbols: int = 20

    def build(self, context: AgentContext) -> PromptBuildResult:
        payload = self._full_payload(context)
        payload_json = self._dump(payload)
        if len(payload_json) <= self.max_chars:
            return PromptBuildResult(
                prompt=self._render_prompt(payload_json),
                payload_json=payload_json,
                trimmed=False,
            )

        compact_payload = self._compact_payload(context)
        compact_json = self._dump(compact_payload)
        if len(compact_json) <= self.max_chars:
            return PromptBuildResult(
                prompt=self._render_prompt(compact_json),
                payload_json=compact_json,
                trimmed=True,
            )

        minimal_payload = self._minimal_payload(context)
        minimal_json = self._dump(minimal_payload)
        return PromptBuildResult(
            prompt=self._render_prompt(minimal_json),
            payload_json=minimal_json,
            trimmed=True,
        )

    def _render_prompt(self, payload_json: str) -> str:
        return (
            "You are a trading policy assistant. Return STRICT JSON only with keys "
            "action, propose_intents, adjust_risk, observe_only, rationale. No markdown.\n"
            f"Context JSON:\n{payload_json}"
        )

    def _dump(self, payload: dict[str, object]) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def _ranked_symbols(self, context: AgentContext, *, max_symbols: int) -> list[str]:
        ranked = sorted(
            context.market_snapshot.keys(),
            key=lambda symbol: (
                context.portfolio.get(symbol, Decimal("0")) * context.market_snapshot.get(symbol, Decimal("0"))
            ),
            reverse=True,
        )
        return ranked[:max_symbols]

    def _base_payload(self, context: AgentContext, *, symbols: list[str]) -> dict[str, object]:
        return {
            "cycle_id": context.cycle_id,
            "generated_at": context.generated_at.isoformat(),
            "is_live_mode": context.is_live_mode,
            "market_data_age_seconds": str(context.market_data_age_seconds),
            "symbols": symbols,
        }

    def _full_payload(self, context: AgentContext) -> dict[str, object]:
        symbols = self._ranked_symbols(context, max_symbols=self.max_symbols)
        payload = self._base_payload(context, symbols=symbols)
        payload.update(
            {
                "market_snapshot": {s: str(context.market_snapshot[s]) for s in symbols},
                "market_spreads_bps": {
                    s: str(context.market_spreads_bps.get(s, Decimal("0"))) for s in symbols
                },
                "portfolio": {s: str(context.portfolio.get(s, Decimal("0"))) for s in symbols},
                "open_orders": context.open_orders,
                "open_orders_count": len(context.open_orders),
                "risk_state": {k: str(v) for k, v in sorted(context.risk_state.items())},
                "recent_events": context.recent_events,
                "recent_events_count": len(context.recent_events),
            }
        )
        return payload

    def _compact_payload(self, context: AgentContext) -> dict[str, object]:
        symbols = self._ranked_symbols(context, max_symbols=min(8, self.max_symbols))
        payload = self._base_payload(context, symbols=symbols)
        payload.update(
            {
                "market_snapshot": {s: str(context.market_snapshot[s]) for s in symbols},
                "market_spreads_bps": {
                    s: str(context.market_spreads_bps.get(s, Decimal("0"))) for s in symbols
                },
                "portfolio": {s: str(context.portfolio.get(s, Decimal("0"))) for s in symbols},
                "open_orders_count": len(context.open_orders),
                "recent_events_count": len(context.recent_events),
                "recent_events_preview": context.recent_events[:3],
                "risk_state": {
                    "kill_switch": str(context.risk_state.get("kill_switch", False)),
                    "safe_mode": str(context.risk_state.get("safe_mode", False)),
                    "drawdown_pct": str(context.risk_state.get("drawdown_pct", Decimal("0"))),
                    "gross_exposure_try": str(
                        context.risk_state.get("gross_exposure_try", Decimal("0"))
                    ),
                },
            }
        )
        return payload

    def _minimal_payload(self, context: AgentContext) -> dict[str, object]:
        symbols = self._ranked_symbols(context, max_symbols=3)
        return {
            **self._base_payload(context, symbols=symbols),
            "market_snapshot": {s: str(context.market_snapshot[s]) for s in symbols},
            "market_spreads_bps": {
                s: str(context.market_spreads_bps.get(s, Decimal("0"))) for s in symbols
            },
            "open_orders_count": len(context.open_orders),
            "recent_events_count": len(context.recent_events),
            "risk_state_keys": sorted(context.risk_state.keys()),
        }


@dataclass(frozen=True)
class RuleBasedPolicy:
    def evaluate(self, context: AgentContext) -> AgentDecision:
        reasons: list[str] = []
        constraints: list[str] = []

        kill_switch = bool(context.risk_state.get("kill_switch", False))
        safe_mode = bool(context.risk_state.get("safe_mode", False))
        stale_threshold = Decimal(str(context.risk_state.get("stale_data_seconds", "0") or "0"))
        stale_data = stale_threshold > Decimal("0") and context.market_data_age_seconds >= stale_threshold

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
                    citations=["risk_state", "market_data_age_seconds"],
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


def sanitize_llm_json(raw: str, *, max_response_chars: int) -> str:
    bounded = raw[:max_response_chars]
    start = bounded.find("{")
    end = bounded.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return bounded
    return bounded[start : end + 1]


@dataclass(frozen=True)
class LlmPolicy:
    client: LlmClient
    prompt_builder: PromptBuilder
    timeout_seconds: float = 3.0
    max_response_chars: int = 6000

    def evaluate(self, context: AgentContext) -> AgentDecision:
        prompt_result = self.prompt_builder.build(context)
        try:
            response = self.client.complete(prompt_result.prompt, timeout_seconds=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            raise LlmPolicyError(f"llm request failed: {type(exc).__name__}") from exc

        sanitized = sanitize_llm_json(response, max_response_chars=self.max_response_chars)
        try:
            parsed = LlmDecisionEnvelope.model_validate_json(sanitized)
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
                    "generated_at": context.generated_at.isoformat(),
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
