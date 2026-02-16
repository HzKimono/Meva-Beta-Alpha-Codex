from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from btcbot.agent.contracts import AgentContext
from btcbot.agent.policy import FallbackPolicy, LlmPolicy, PromptBuilder, RuleBasedPolicy


class BadClient:
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        raise TimeoutError("timeout")


class GoodClient:
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        return (
            '{"action":"propose_intents","propose_intents":[],"adjust_risk":{},'
            '"observe_only":false,"rationale":{"reasons":["ok"],"confidence":0.8,'
            '"constraints_hit":[],"citations":["market_snapshot"]}}'
        )


class InvalidJsonClient:
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        return '{"action":"unknown"}'


def _context() -> AgentContext:
    return AgentContext(
        cycle_id="cycle-1",
        generated_at=datetime.now(UTC),
        market_snapshot={"BTCTRY": Decimal("100")},
        market_spreads_bps={"BTCTRY": Decimal("10")},
        portfolio={"TRY": Decimal("1000")},
        open_orders=[],
        risk_state={"kill_switch": False, "safe_mode": False},
        recent_events=[],
        started_at=datetime.now(UTC),
        is_live_mode=False,
    )


def test_llm_schema_validation_failure() -> None:
    policy = LlmPolicy(client=InvalidJsonClient(), prompt_builder=PromptBuilder())
    with pytest.raises(Exception):
        policy.evaluate(_context())


def test_fallback_policy_uses_rule_based_on_llm_failure() -> None:
    fallback = FallbackPolicy(
        primary=LlmPolicy(client=BadClient(), prompt_builder=PromptBuilder()),
        fallback=RuleBasedPolicy(),
    )
    decision = fallback.evaluate(_context())
    assert decision.action.value == "no_op"


def test_prompt_builder_token_bound() -> None:
    context = _context().model_copy(update={"recent_events": ["x" * 5000]})
    built = PromptBuilder(max_chars=300).build(context)
    assert built.trimmed is True
    assert len(built.prompt) > 0


def test_llm_policy_parses_valid_response() -> None:
    policy = LlmPolicy(client=GoodClient(), prompt_builder=PromptBuilder())
    decision = policy.evaluate(_context())
    assert decision.action.value == "propose_intents"
