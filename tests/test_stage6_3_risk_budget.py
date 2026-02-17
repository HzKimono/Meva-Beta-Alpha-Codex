from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.agent.contracts import AgentDecision, DecisionAction, DecisionRationale, SafeDecision
from btcbot.config import Settings
from btcbot.domain.risk_budget import Mode, RiskDecision, RiskLimits, RiskSignals, decide_mode
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType
from btcbot.risk.budget import RiskBudgetView
from btcbot.services import stage4_cycle_runner as runner_module
from btcbot.services.ledger_service import PnlReport
from btcbot.services.risk_budget_service import BudgetDecision, RiskBudgetService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.state_store import StateStore


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


def _budget_decision(
    mode: Mode = Mode.NORMAL, multiplier: Decimal = Decimal("1")
) -> BudgetDecision:
    return BudgetDecision(
        risk_decision=RiskDecision(
            mode=mode,
            reasons=["TEST"],
            limits=_limits(),
            signals=_signals(),
            decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        budget_view=RiskBudgetView(
            trading_capital_try=Decimal("1000"),
            treasury_try=Decimal("0"),
            available_risk_capital_try=Decimal("1000"),
            daily_loss_limit_try=Decimal("50"),
            drawdown_halt_limit_try=Decimal("150"),
            max_gross_exposure_try=Decimal("1200") * multiplier,
            max_order_notional_try=Decimal("200") * multiplier,
            position_sizing_multiplier=multiplier,
            mode=mode,
        ),
    )


def test_decide_mode_is_deterministic_for_same_inputs() -> None:
    first = decide_mode(_limits(), _signals(drawdown_try=Decimal("250")))
    second = decide_mode(_limits(), _signals(drawdown_try=Decimal("250")))
    assert first == second
    assert first == (Mode.OBSERVE_ONLY, ["DRAWDOWN_LIMIT"])


def test_decide_mode_exposure_triggers_reduce_risk_only() -> None:
    mode, reasons = decide_mode(_limits(), _signals(gross_exposure_try=Decimal("600")))
    assert mode == Mode.REDUCE_RISK_ONLY
    assert reasons == ["EXPOSURE_LIMIT"]


def test_decide_mode_fee_budget_triggers_reduce_risk_only() -> None:
    mode, reasons = decide_mode(_limits(), _signals(fees_try_today=Decimal("11")))
    assert mode == Mode.REDUCE_RISK_ONLY
    assert reasons == ["FEE_BUDGET"]


def test_decide_mode_ok_triggers_normal() -> None:
    mode, reasons = decide_mode(_limits(), _signals(drawdown_try=Decimal("10")))
    assert mode == Mode.NORMAL
    assert reasons == ["OK"]


def test_risk_budget_service_fees_is_idempotent(tmp_path) -> None:
    db = StateStore(str(tmp_path / "risk.sqlite"))
    fixed_now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    service = RiskBudgetService(db, now_provider=lambda: fixed_now)

    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("12.5")},
        per_symbol=[],
        equity_estimate=Decimal("1000"),
    )

    first, _, peak_first, fees_first, day_first = service.compute_decision(
        limits=_limits(),
        pnl_report=pnl_report,
        positions=[],
        mark_prices={},
        realized_today_try=Decimal("0"),
        kill_switch_active=False,
    )
    service.persist_decision(
        cycle_id="c1",
        decision=first,
        prev_mode=None,
        peak_equity=peak_first,
        peak_day=day_first,
        fees_today=fees_first,
        fees_day=day_first,
    )
    second, _, _, fees_second, _ = service.compute_decision(
        limits=_limits(),
        pnl_report=pnl_report,
        positions=[],
        mark_prices={},
        realized_today_try=Decimal("0"),
        kill_switch_active=False,
    )

    assert first.risk_decision.signals.fees_try_today == Decimal("12.5")
    assert second.risk_decision.signals.fees_try_today == Decimal("12.5")
    assert fees_first == fees_second


def test_compute_decision_invalid_stored_mode_is_safe() -> None:
    class FakeStore:
        def get_risk_state_current(self):
            return {
                "current_mode": "BROKEN_VALUE",
                "peak_equity_try": "1000",
                "peak_equity_date": "2026-01-01",
                "fees_try_today": "0",
                "fees_day": "2026-01-01",
            }

    fixed_now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    service = RiskBudgetService(FakeStore(), now_provider=lambda: fixed_now)  # type: ignore[arg-type]
    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("1")},
        per_symbol=[],
        equity_estimate=Decimal("1000"),
    )

    decision, prev_mode, *_ = service.compute_decision(
        limits=_limits(),
        pnl_report=pnl_report,
        positions=[],
        mark_prices={},
        realized_today_try=Decimal("0"),
        kill_switch_active=False,
    )

    assert prev_mode is None
    assert isinstance(decision.risk_decision, RiskDecision)
    assert decision.mode in (Mode.NORMAL, Mode.REDUCE_RISK_ONLY, Mode.OBSERVE_ONLY)


def test_risk_budget_service_persist_is_single_atomic_call() -> None:
    calls: list[dict[str, object]] = []

    class FakeStore:
        def persist_risk(self, **kwargs):
            calls.append(kwargs)

    service = RiskBudgetService(FakeStore())  # type: ignore[arg-type]
    decision = _budget_decision(Mode.NORMAL, Decimal("1"))

    service.persist_decision(
        cycle_id="cycle-1",
        decision=decision,
        prev_mode=Mode.REDUCE_RISK_ONLY,
        peak_equity=Decimal("1000"),
        peak_day=datetime(2026, 1, 1, tzinfo=UTC).date(),
        fees_today=Decimal("1"),
        fees_day=datetime(2026, 1, 1, tzinfo=UTC).date(),
    )

    assert len(calls) == 1
    assert calls[0]["cycle_id"] == "cycle-1"


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
            return (
                _budget_decision(self.mode, Decimal("0.3")),
                None,
                Decimal("1000"),
                Decimal("0"),
                datetime(2026, 1, 1, tzinfo=UTC).date(),
            )

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


def test_apply_agent_policy_scales_guardrails_with_budget_multiplier(monkeypatch) -> None:
    captured: dict[str, Decimal] = {}

    class FakeSafetyGuard:
        def __init__(self, **kwargs) -> None:
            captured["max_exposure_try"] = kwargs["max_exposure_try"]
            captured["max_order_notional_try"] = kwargs["max_order_notional_try"]

        def apply(self, context, decision):
            del context
            return SafeDecision(decision=decision)

    class FakeAuditTrail:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def persist(self, **kwargs) -> None:
            del kwargs
            return

    class FakePolicy:
        def evaluate(self, context):
            del context
            return AgentDecision(
                action=DecisionAction.NO_OP,
                propose_intents=[],
                rationale=DecisionRationale(
                    reasons=["test"],
                    confidence=1.0,
                    constraints_hit=[],
                    citations=["test"],
                ),
            )

    class FakeStore:
        def get_latest_risk_mode(self):
            raise AssertionError("latest mode should not be queried for guard multiplier")

    monkeypatch.setattr(runner_module, "SafetyGuard", FakeSafetyGuard)
    monkeypatch.setattr(runner_module, "AgentAuditTrail", FakeAuditTrail)

    runner = Stage4CycleRunner()
    monkeypatch.setattr(
        runner_module.Stage4CycleRunner,
        "_resolve_agent_policy",
        lambda self, settings: FakePolicy(),
    )

    now = datetime(2026, 1, 1, tzinfo=UTC)
    order = runner_module.Order(
        symbol="BTCTRY",
        side="buy",
        type="limit",
        price=Decimal("100"),
        qty=Decimal("0.1"),
        status="new",
        created_at=now,
        updated_at=now,
        client_order_id="cid-1",
    )

    runner._apply_agent_policy(
        settings=Settings(
            AGENT_POLICY_ENABLED=True,
            RISK_MAX_GROSS_EXPOSURE_TRY=Decimal("1000"),
            RISK_MAX_ORDER_NOTIONAL_TRY=Decimal("500"),
            AGENT_MAX_ORDER_NOTIONAL_TRY=Decimal("0"),
        ),
        state_store=FakeStore(),
        cycle_id="c1",
        cycle_started_at=now,
        cycle_now=now,
        intents=[order],
        mark_prices={"BTCTRY": Decimal("100")},
        market_spreads_bps={"BTCTRY": Decimal("2")},
        market_data_age_seconds=Decimal("1"),
        positions=[],
        current_open_orders=[],
        snapshot=type("Snap", (), {"drawdown_pct": Decimal("0")})(),
        live_mode=False,
        failed_symbols=set(),
        budget_guard_multiplier=Decimal("0.25"),
    )

    assert captured["max_exposure_try"] == Decimal("250")
    assert captured["max_order_notional_try"] == Decimal("125")
