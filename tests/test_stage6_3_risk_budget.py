from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.risk_budget import Mode, RiskDecision, RiskLimits, RiskSignals, decide_mode
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType
from btcbot.services import stage4_cycle_runner as runner_module
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner


def _limits() -> RiskLimits:
    return RiskLimits(
        max_daily_drawdown_try=Decimal("100"),
        max_drawdown_try=Decimal("200"),
        max_gross_exposure_try=Decimal("500"),
        max_position_pct=Decimal("0.4"),
        max_order_notional_try=Decimal("100"),
        max_fee_try_per_day=Decimal("10"),
    )


def _signals(**overrides: Decimal) -> RiskSignals:
    base = dict(
        equity_try=Decimal("1000"),
        peak_equity_try=Decimal("1100"),
        drawdown_try=Decimal("100"),
        daily_pnl_try=Decimal("20"),
        gross_exposure_try=Decimal("200"),
        largest_position_pct=Decimal("0.2"),
        fees_try_today=Decimal("1"),
    )
    base.update(overrides)
    return RiskSignals(**base)


def test_decide_mode_drawdown_triggers_observe_only() -> None:
    decision = decide_mode(_limits(), _signals(drawdown_try=Decimal("250")))
    assert decision.mode == Mode.OBSERVE_ONLY
    assert decision.reasons == ["DRAWDOWN_LIMIT"]


def test_decide_mode_exposure_triggers_reduce_risk_only() -> None:
    decision = decide_mode(_limits(), _signals(gross_exposure_try=Decimal("600")))
    assert decision.mode == Mode.REDUCE_RISK_ONLY
    assert decision.reasons == ["EXPOSURE_LIMIT"]


def test_decide_mode_fee_budget_triggers_reduce_risk_only() -> None:
    decision = decide_mode(_limits(), _signals(fees_try_today=Decimal("11")))
    assert decision.mode == Mode.REDUCE_RISK_ONLY
    assert decision.reasons == ["FEE_BUDGET"]


def test_decide_mode_ok_triggers_normal() -> None:
    decision = decide_mode(_limits(), _signals(drawdown_try=Decimal("10")))
    assert decision.mode == Mode.NORMAL
    assert decision.reasons == ["OK"]


class _FakeExchange:
    def get_orderbook(self, symbol: str):
        del symbol
        return (100.0, 101.0)

    def get_balances(self):
        return [type("B", (), {"asset": "TRY", "free": Decimal("1000")})()]

    def list_open_orders(self, symbol: str):
        del symbol
        return []

    def get_recent_fills(self, symbol: str, since_ms: int | None = None):
        del symbol, since_ms
        return []

    def get_exchange_info(self):
        return []

    def close(self) -> None:
        return


def test_runner_mode_gating(monkeypatch, tmp_path) -> None:
    exchange = _FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    captured: dict[str, list[LifecycleAction]] = {}

    class FakeLifecycle:
        def __init__(self, stale_after_sec: int) -> None:
            del stale_after_sec

        def plan(self, intents, current_open_orders, mid_price):
            del intents, current_open_orders, mid_price
            actions = [
                LifecycleAction(
                    action_type=LifecycleActionType.SUBMIT,
                    symbol="BTCTRY",
                    side="BUY",
                    price=Decimal("100"),
                    qty=Decimal("0.1"),
                    reason="test",
                    client_order_id="buy-1",
                ),
                LifecycleAction(
                    action_type=LifecycleActionType.SUBMIT,
                    symbol="BTCTRY",
                    side="SELL",
                    price=Decimal("100"),
                    qty=Decimal("0.1"),
                    reason="test",
                    client_order_id="sell-1",
                ),
                LifecycleAction(
                    action_type=LifecycleActionType.CANCEL,
                    symbol="BTCTRY",
                    side="SELL",
                    price=Decimal("100"),
                    qty=Decimal("0.1"),
                    reason="test",
                    client_order_id="sell-1",
                ),
            ]
            return type("P", (), {"actions": actions, "audit_reasons": []})()

    class FakeRiskPolicy:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter_actions(self, actions, **kwargs):
            del kwargs
            return actions, []

    class FakeExecution:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def execute_with_report(self, actions):
            captured["actions"] = list(actions)
            return type(
                "ER",
                (),
                {"executed_total": 0, "submitted": 0, "canceled": 0, "simulated": 0, "rejected": 0},
            )()

    class FakeRiskBudgetService:
        mode = Mode.NORMAL

        def __init__(self, state_store) -> None:
            del state_store

        def compute_decision(self, **kwargs):
            del kwargs
            decision = RiskDecision(
                mode=self.mode,
                reasons=["TEST"],
                limits=_limits(),
                signals=_signals(),
                decided_at=datetime.now(UTC),
            )
            return decision, None, Decimal("1000"), Decimal("0"), datetime.now(UTC).date()

        def persist_decision(self, **kwargs) -> None:
            del kwargs
            return

    monkeypatch.setattr(runner_module, "OrderLifecycleService", FakeLifecycle)
    monkeypatch.setattr(runner_module, "RiskPolicy", FakeRiskPolicy)
    monkeypatch.setattr(runner_module, "ExecutionService", FakeExecution)
    monkeypatch.setattr(runner_module, "RiskBudgetService", FakeRiskBudgetService)

    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(tmp_path / "risk.sqlite")
    )
    runner = Stage4CycleRunner()

    FakeRiskBudgetService.mode = Mode.OBSERVE_ONLY
    assert runner.run_one_cycle(settings) == 0
    assert captured["actions"] == []

    FakeRiskBudgetService.mode = Mode.REDUCE_RISK_ONLY
    assert runner.run_one_cycle(settings) == 0
    assert all(
        action.action_type == LifecycleActionType.CANCEL or action.side == "SELL"
        for action in captured["actions"]
    )
    assert all(
        not (action.action_type == LifecycleActionType.SUBMIT and action.side == "BUY")
        for action in captured["actions"]
    )

    FakeRiskBudgetService.mode = Mode.NORMAL
    assert runner.run_one_cycle(settings) == 0
    assert any(
        action.action_type == LifecycleActionType.SUBMIT and action.side == "BUY"
        for action in captured["actions"]
    )
