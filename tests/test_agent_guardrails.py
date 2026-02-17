from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.agent.contracts import AgentContext, AgentDecision, DecisionAction, DecisionRationale
from btcbot.agent.guardrails import SafetyGuard


def _context() -> AgentContext:
    frozen = datetime(2025, 1, 1, tzinfo=UTC)
    return AgentContext(
        cycle_id="c1",
        generated_at=frozen,
        market_snapshot={"BTCTRY": Decimal("100")},
        market_spreads_bps={"BTCTRY": Decimal("20")},
        market_data_age_seconds=Decimal("1"),
        portfolio={"BTCTRY": Decimal("0.1")},
        open_orders=[],
        risk_state={"drawdown_pct": Decimal("1"), "gross_exposure_try": Decimal("100")},
        recent_events=[],
        started_at=frozen,
        is_live_mode=False,
    )


def _decision() -> AgentDecision:
    return AgentDecision(
        action=DecisionAction.PROPOSE_INTENTS,
        rationale=DecisionRationale(
            reasons=["x"], confidence=0.9, citations=[], constraints_hit=[]
        ),
    )


def test_guard_kill_switch_forces_observe_only() -> None:
    guard = SafetyGuard(
        max_exposure_try=Decimal("1000"),
        max_order_notional_try=Decimal("1000"),
        max_drawdown_pct=Decimal("10"),
        min_notional_try=Decimal("10"),
        max_spread_bps=Decimal("100"),
        symbol_allowlist={"BTCTRY"},
        cooldown_seconds=60,
        stale_data_seconds=30,
        kill_switch=True,
        safe_mode=False,
        observe_only_override=False,
    )
    safe = guard.apply(_context(), _decision())
    assert safe.decision.observe_only is True
    assert "kill_switch" in safe.blocked_reasons


def test_guard_stale_data_uses_typed_context_signal() -> None:
    guard = SafetyGuard(
        max_exposure_try=Decimal("1000"),
        max_order_notional_try=Decimal("1000"),
        max_drawdown_pct=Decimal("10"),
        min_notional_try=Decimal("10"),
        max_spread_bps=Decimal("100"),
        symbol_allowlist={"BTCTRY"},
        cooldown_seconds=60,
        stale_data_seconds=30,
        kill_switch=False,
        safe_mode=False,
        observe_only_override=False,
    )
    context = _context().model_copy(update={"market_data_age_seconds": Decimal("31")})
    safe = guard.apply(context, _decision())
    assert safe.decision.observe_only is True
    assert "stale_data" in safe.blocked_reasons


def test_guard_records_spread_drop_reason() -> None:
    guard = SafetyGuard(
        max_exposure_try=Decimal("1000"),
        max_order_notional_try=Decimal("200"),
        max_drawdown_pct=Decimal("10"),
        min_notional_try=Decimal("10"),
        max_spread_bps=Decimal("15"),
        symbol_allowlist={"BTCTRY"},
        cooldown_seconds=60,
        stale_data_seconds=30,
        kill_switch=False,
        safe_mode=False,
        observe_only_override=False,
    )
    decision = AgentDecision.model_validate(
        {
            "action": "propose_intents",
            "propose_intents": [
                {
                    "symbol": "BTCTRY",
                    "side": "BUY",
                    "price_try": "100",
                    "qty": "1",
                    "notional_try": "100",
                    "reason": "test",
                }
            ],
            "rationale": {
                "reasons": ["x"],
                "confidence": 0.5,
                "constraints_hit": [],
                "citations": [],
            },
        }
    )
    safe = guard.apply(_context(), decision)
    assert safe.decision.propose_intents == []
    assert safe.diff["dropped_by_symbol"]["BTCTRY"] == ["spread_too_wide"]
