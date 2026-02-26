from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_models import ExposureSnapshot
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
    )
    return Stage7RiskInputs(**{**base.__dict__, **overrides})


def test_mode_selection_stale_data_observe_only() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, LIVE_TRADING=False, KILL_SWITCH=False)
    decision = Stage7RiskBudgetService().decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=_inputs(market_data_age_sec=10_000),
        previous_decision=None,
    )
    assert decision.mode == Mode.OBSERVE_ONLY
    assert "STALE_DATA" in decision.reasons


def test_mode_selection_loss_breach_reduce_risk_only() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, LIVE_TRADING=False, KILL_SWITCH=False)
    decision = Stage7RiskBudgetService().decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=_inputs(daily_pnl_try=Decimal("-1000")),
        previous_decision=None,
    )
    assert decision.mode == Mode.REDUCE_RISK_ONLY
    assert "LOSS_LIMIT" in decision.reasons


def test_caps_shrink_when_high_volatility() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    decision = Stage7RiskBudgetService().decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=_inputs(observed_spread_bps=settings.high_vol_threshold_bps),
        previous_decision=None,
    )
    assert decision.max_order_notional_try < settings.max_order_notional_try
    assert decision.max_orders_per_cycle <= settings.max_orders_per_cycle
