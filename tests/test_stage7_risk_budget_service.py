from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_engine import CycleRiskOutput
from btcbot.domain.risk_models import ExposureSnapshot
from btcbot.services.risk_policy_service import ActionPortfolioSnapshot, RiskPolicyService
from btcbot.services.stage7_cycle_runner import _filter_order_intents_by_risk
from btcbot.services.stage7_risk_budget_service import Stage7RiskBudgetService, Stage7RiskInputs


def _snapshot() -> ExposureSnapshot:
    return ExposureSnapshot(
        per_symbol_exposure_try={"BTCTRY": Decimal("100")},
        total_exposure_try=Decimal("100"),
        concentration_top_n=[("BTCTRY", Decimal("100"))],
        turnover_estimate_try=Decimal("0"),
        free_cash_try=Decimal("900"),
        computed_at=datetime(2024, 1, 1, tzinfo=UTC),
        inputs_hash="h1",
    )


def _inputs(**overrides: object) -> Stage7RiskInputs:
    base = Stage7RiskInputs(
        max_drawdown_pct=Decimal("0.01"),
        daily_pnl_try=Decimal("10"),
        consecutive_loss_streak=0,
        market_data_age_sec=1,
        observed_spread_bps=Decimal("10"),
        quote_volume_try=Decimal("10000"),
        exposure_snapshot=_snapshot(),
        fee_burn_today_try=Decimal("1"),
    )
    return Stage7RiskInputs(**{**base.__dict__, **overrides})


def _settings(**kwargs: object) -> Settings:
    base = {
        "DRY_RUN": True,
        "STAGE7_ENABLED": True,
        "LIVE_TRADING": False,
        "KILL_SWITCH": False,
        "DAILY_LOSS_LIMIT_TRY": "100",
        "MAX_DRAWDOWN_BPS": "500",
        "FEE_BURN_LIMIT_TRY": "10",
        "HIGH_VOL_THRESHOLD_BPS": "250",
        "MAX_ORDER_NOTIONAL_TRY": "3000",
        "MAX_ORDERS_PER_CYCLE": 4,
        "MAX_SYMBOL_EXPOSURE_TRY": "5000",
    }
    base.update(kwargs)
    return Settings(**base)


def test_mode_selection_stale_data_observe_only() -> None:
    settings = _settings(STAGE7_MAX_DATA_AGE_SEC=30)
    decision = Stage7RiskBudgetService().decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=_inputs(market_data_age_sec=10_000),
        previous_decision=None,
    )
    assert decision.mode == Mode.OBSERVE_ONLY
    assert "STALE_DATA" in decision.reasons


def test_mode_selection_loss_breach_reduce_risk_only() -> None:
    settings = _settings(DAILY_LOSS_LIMIT_TRY="100")
    decision = Stage7RiskBudgetService().decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=_inputs(daily_pnl_try=Decimal("-200")),
        previous_decision=None,
    )
    assert decision.mode == Mode.REDUCE_RISK_ONLY
    assert "LOSS_LIMIT" in decision.reasons


def test_caps_shrink_when_high_volatility() -> None:
    settings = _settings(HIGH_VOL_THRESHOLD_BPS="200", MAX_ORDERS_PER_CYCLE=6)
    decision = Stage7RiskBudgetService().decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=_inputs(observed_spread_bps=Decimal("200")),
        previous_decision=None,
    )
    assert decision.max_order_notional_try < settings.max_order_notional_try
    assert decision.max_orders_per_cycle < settings.max_orders_per_cycle


def test_planning_path_order_intents_are_risk_filtered_and_recorded() -> None:
    cycle_risk = CycleRiskOutput(
        mode=Mode.REDUCE_RISK_ONLY,
        reasons=["LOSS_LIMIT"],
        max_order_notional_try=Decimal("1000"),
        max_orders_per_cycle=1,
        max_symbol_exposure_try=Decimal("10000"),
        daily_loss_limit_try=Decimal("100"),
        max_drawdown_bps=500,
        fee_burn_limit_try=Decimal("10"),
        cooldown_until_utc=None,
        allow_submit=True,
        allow_cancel=True,
        decided_at=datetime(2024, 1, 1, tzinfo=UTC),
        inputs_hash="x",
        metrics={},
    )
    intents = [
        OrderIntent(
            cycle_id="c1",
            symbol="BTCTRY",
            side="BUY",
            order_type="LIMIT",
            price_try=Decimal("100"),
            qty=Decimal("1"),
            notional_try=Decimal("100"),
            client_order_id="buy-1",
            reason="test",
            constraints_applied={},
        ),
        OrderIntent(
            cycle_id="c1",
            symbol="BTCTRY",
            side="SELL",
            order_type="LIMIT",
            price_try=Decimal("100"),
            qty=Decimal("1"),
            notional_try=Decimal("100"),
            client_order_id="sell-1",
            reason="test",
            constraints_applied={},
        ),
    ]

    filtered, skipped_actions = _filter_order_intents_by_risk(
        order_intents=intents,
        risk_policy_service=RiskPolicyService(),
        portfolio_snapshot=ActionPortfolioSnapshot(positions_by_symbol={"BTCTRY": Decimal("2")}),
        cycle_risk=cycle_risk,
    )

    assert filtered[0].skipped is True
    assert filtered[0].skip_reason == "RISK_REDUCE_ONLY_BLOCKED"
    assert filtered[1].skipped is False
    assert skipped_actions[0]["reason"] == "RISK_REDUCE_ONLY_BLOCKED"
