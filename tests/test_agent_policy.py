from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.agent.contracts import AgentContext
from btcbot.agent.policy import (
    FallbackPolicy,
    LlmPolicy,
    LlmPolicyError,
    PromptBuilder,
    RuleBasedPolicy,
    sanitize_llm_json,
)


class BadClient:
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        raise TimeoutError("timeout")


class GoodClient:
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        return (
            'prefix{"action":"propose_intents","propose_intents":[],"adjust_risk":{},'
            '"observe_only":false,"rationale":{"reasons":["ok"],"confidence":0.8,'
            '"constraints_hit":[],"citations":["market_snapshot"]}}suffix'
        )


class InvalidJsonClient:
    def complete(self, prompt: str, *, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        return '{"action":"unknown"}'


def _context() -> AgentContext:
    frozen = datetime(2025, 1, 1, tzinfo=UTC)
    return AgentContext(
        cycle_id="cycle-1",
        generated_at=frozen,
        market_snapshot={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("200")},
        market_spreads_bps={"BTCTRY": Decimal("10"), "ETHTRY": Decimal("20")},
        market_data_age_seconds=Decimal("5"),
        portfolio={"BTCTRY": Decimal("1"), "ETHTRY": Decimal("2"), "TRY": Decimal("1000")},
        open_orders=[{"symbol": "BTCTRY", "side": "BUY", "qty": "0.1", "price": "100"}],
        risk_state={"kill_switch": False, "safe_mode": False, "drawdown_pct": Decimal("1")},
        recent_events=["event" for _ in range(20)],
        started_at=frozen,
        is_live_mode=False,
    )


def test_llm_schema_validation_failure() -> None:
    policy = LlmPolicy(client=InvalidJsonClient(), prompt_builder=PromptBuilder())
    with pytest.raises(LlmPolicyError):
        policy.evaluate(_context())


def test_fallback_policy_uses_rule_based_on_llm_failure() -> None:
    fallback = FallbackPolicy(
        primary=LlmPolicy(client=BadClient(), prompt_builder=PromptBuilder()),
        fallback=RuleBasedPolicy(),
    )
    decision = fallback.evaluate(_context())
    assert decision.action.value == "no_op"


def test_prompt_builder_is_structural_and_valid_json() -> None:
    context = _context().model_copy(update={"recent_events": ["x" * 2000 for _ in range(10)]})
    built = PromptBuilder(max_chars=500).build(context)
    assert built.trimmed is True
    payload = json.loads(built.payload_json)
    assert isinstance(payload, dict)
    assert "cycle_id" in payload
    assert "open_orders_count" in payload


def test_llm_policy_parses_valid_response_with_sanitization() -> None:
    policy = LlmPolicy(client=GoodClient(), prompt_builder=PromptBuilder())
    decision = policy.evaluate(_context())
    assert decision.action.value == "propose_intents"


def test_sanitize_llm_json_extracts_braced_payload() -> None:
    sanitized = sanitize_llm_json(
        'garbage{"action":"no_op","propose_intents":[],"adjust_risk":{},"observe_only":false,'
        '"rationale":{"reasons":[],"confidence":0.1,"constraints_hit":[],"citations":[]}}tail',
        max_response_chars=2000,
    )
    assert sanitized.startswith("{")
    assert sanitized.endswith("}")
