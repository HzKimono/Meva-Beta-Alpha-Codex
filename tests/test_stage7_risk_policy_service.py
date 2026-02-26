from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_engine import CycleRiskOutput
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType
from btcbot.services.risk_policy_service import ActionPortfolioSnapshot, RiskPolicyService


def _risk_output(mode: Mode) -> CycleRiskOutput:
    return CycleRiskOutput(
        mode=mode,
        reasons=["x"],
        max_order_notional_try=Decimal("1000"),
        max_orders_per_cycle=5,
        max_symbol_exposure_try=Decimal("2000"),
        daily_loss_limit_try=Decimal("500"),
        max_drawdown_bps=2500,
        fee_burn_limit_try=Decimal("100"),
        cooldown_until_utc=None,
        allow_submit=(mode != Mode.OBSERVE_ONLY),
        allow_cancel=True,
        decided_at=datetime(2024, 1, 1, tzinfo=UTC),
        inputs_hash="h",
        metrics={},
    )


def test_reduce_risk_only_blocks_risk_increasing_submits_allows_cancels() -> None:
    svc = RiskPolicyService()
    actions = [
        LifecycleAction(LifecycleActionType.CANCEL, "BTCTRY", "SELL", Decimal("100"), Decimal("1"), "x"),
        LifecycleAction(LifecycleActionType.SUBMIT, "BTCTRY", "BUY", Decimal("100"), Decimal("1"), "x"),
        LifecycleAction(LifecycleActionType.SUBMIT, "BTCTRY", "SELL", Decimal("100"), Decimal("1"), "x"),
    ]
    accepted, decisions = svc.filter_actions(
        actions=actions,
        portfolio=ActionPortfolioSnapshot(positions_by_symbol={"BTCTRY": Decimal("2")}),
        cycle_risk=_risk_output(Mode.REDUCE_RISK_ONLY),
    )

    assert actions[0] in accepted
    assert actions[2] in accepted
    assert actions[1] not in accepted
    assert any(d.reason == "mode_reduce_risk_only" for d in decisions)
