from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.agent.audit import AgentAuditTrail, redact_secrets
from btcbot.agent.contracts import AgentContext, AgentDecision, DecisionAction, DecisionRationale, SafeDecision
from btcbot.agent.policy import RuleBasedPolicy
from btcbot.services.state_store import StateStore


def _context() -> AgentContext:
    frozen = datetime(2025, 1, 1, tzinfo=UTC)
    return AgentContext(
        cycle_id="replay-cycle",
        generated_at=frozen,
        market_snapshot={"BTCTRY": Decimal("100")},
        market_spreads_bps={"BTCTRY": Decimal("10")},
        portfolio={"TRY": Decimal("250")},
        open_orders=[],
        risk_state={"kill_switch": False, "safe_mode": False, "api_key": "abc"},
        recent_events=["evt1"],
        started_at=frozen,
        is_live_mode=False,
    )


def test_redact_secrets_nested() -> None:
    payload = {"outer": {"api_key": "abc", "token": "def"}, "ok": 1}
    redacted = redact_secrets(payload)
    assert redacted["outer"]["api_key"] == "***REDACTED***"
    assert redacted["outer"]["token"] == "***REDACTED***"


def test_agent_audit_persists_rows(tmp_path) -> None:
    store = StateStore(str(tmp_path / "agent.sqlite"))
    context = _context()
    decision = AgentDecision(
        action=DecisionAction.NO_OP,
        rationale=DecisionRationale(reasons=["noop"], confidence=0.7, constraints_hit=[], citations=[]),
    )
    safe_decision = SafeDecision(decision=decision)
    AgentAuditTrail(state_store=store).persist(
        cycle_id="replay-cycle",
        correlation_id="corr-1",
        context=context,
        decision=decision,
        safe_decision=safe_decision,
    )
    with store._connect() as conn:
        row = conn.execute("SELECT cycle_id, correlation_id FROM agent_decision_audit").fetchone()
    assert row is not None
    assert row["cycle_id"] == "replay-cycle"


def test_rule_policy_replay_is_deterministic_for_frozen_context() -> None:
    context = _context()
    policy = RuleBasedPolicy()
    first = policy.evaluate(context)
    second = policy.evaluate(context)
    assert first == second
