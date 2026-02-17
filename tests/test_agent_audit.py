from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.agent.audit import AgentAuditTrail, redact_secrets, store_compact_text
from btcbot.agent.contracts import (
    AgentContext,
    AgentDecision,
    DecisionAction,
    DecisionRationale,
    SafeDecision,
)
from btcbot.agent.policy import RuleBasedPolicy
from btcbot.services.state_store import StateStore


def _context() -> AgentContext:
    frozen = datetime(2025, 1, 1, tzinfo=UTC)
    return AgentContext(
        cycle_id="replay-cycle",
        generated_at=frozen,
        market_snapshot={"BTCTRY": Decimal("100")},
        market_spreads_bps={"BTCTRY": Decimal("10")},
        market_data_age_seconds=Decimal("5"),
        portfolio={"TRY": Decimal("250")},
        open_orders=[],
        risk_state={"kill_switch": False, "safe_mode": False, "api_key": "abc"},
        recent_events=["evt1"],
        started_at=frozen,
        is_live_mode=False,
    )


def _decision(confidence: float = 0.7) -> AgentDecision:
    return AgentDecision(
        action=DecisionAction.NO_OP,
        rationale=DecisionRationale(
            reasons=["noop"],
            confidence=confidence,
            constraints_hit=[],
            citations=[],
        ),
    )


def test_redact_secrets_nested() -> None:
    payload = {"outer": {"api_key": "abc", "token": "def"}, "ok": 1}
    redacted = redact_secrets(payload)
    assert redacted["outer"]["api_key"] == "***REDACTED***"
    assert redacted["outer"]["token"] == "***REDACTED***"


def test_store_compact_text_uses_head_tail_wrapper() -> None:
    compact = store_compact_text("x" * 100, max_chars=20)
    assert compact["truncated"] is True
    assert "head" in compact and "tail" in compact
    assert len(compact["head"] + compact["tail"]) <= 20


def test_agent_audit_persists_idempotent_rows(tmp_path) -> None:
    store = StateStore(str(tmp_path / "agent.sqlite"))
    context = _context()
    first = _decision(0.7)
    second = _decision(0.9)
    safe_first = SafeDecision(decision=first)
    safe_second = SafeDecision(decision=second)
    trail = AgentAuditTrail(state_store=store)

    trail.persist(
        cycle_id="replay-cycle",
        correlation_id="corr-1",
        context=context,
        decision=first,
        safe_decision=safe_first,
    )
    trail.persist(
        cycle_id="replay-cycle",
        correlation_id="corr-1",
        context=context,
        decision=second,
        safe_decision=safe_second,
    )

    with store._connect() as conn:
        row_count = conn.execute("SELECT COUNT(*) as c FROM agent_decision_audit").fetchone()["c"]
        row = conn.execute(
            "SELECT decision_json FROM agent_decision_audit WHERE cycle_id=? AND correlation_id=?",
            ("replay-cycle", "corr-1"),
        ).fetchone()

    assert row_count == 1
    wrapped_decision = json.loads(str(row["decision_json"]))
    assert wrapped_decision["is_json"] is True


def test_agent_audit_prompt_response_bounded(tmp_path) -> None:
    store = StateStore(str(tmp_path / "agent2.sqlite"))
    trail = AgentAuditTrail(state_store=store, include_prompt_payloads=True, max_payload_chars=32)
    decision = _decision()
    safe_decision = SafeDecision(decision=decision)
    trail.persist(
        cycle_id="replay-cycle",
        correlation_id="corr-2",
        context=_context(),
        decision=decision,
        safe_decision=safe_decision,
        prompt="p" * 200,
        response="r" * 200,
    )
    with store._connect() as conn:
        row = conn.execute(
            "SELECT prompt_json, response_json FROM agent_decision_audit "
            "WHERE correlation_id='corr-2'"
        ).fetchone()
    prompt_payload = json.loads(str(row["prompt_json"]))
    response_payload = json.loads(str(row["response_json"]))
    assert prompt_payload["truncated"] is True
    assert response_payload["truncated"] is True


def test_rule_policy_replay_is_deterministic_for_frozen_context() -> None:
    context = _context()
    policy = RuleBasedPolicy()
    first = policy.evaluate(context)
    second = policy.evaluate(context)
    assert first == second
