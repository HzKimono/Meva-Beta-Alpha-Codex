from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.risk_models import ExposureSnapshot, RiskDecision, RiskMode
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


def _inputs() -> Stage7RiskInputs:
    return Stage7RiskInputs(
        max_drawdown_pct=Decimal("0.01"),
        daily_pnl_try=Decimal("10"),
        consecutive_loss_streak=0,
        market_data_age_sec=1,
        observed_spread_bps=Decimal("10"),
        quote_volume_try=Decimal("10000"),
        exposure_snapshot=_snapshot(),
    )


def test_drawdown_breach_sets_observe_only_with_cooldown() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    svc = Stage7RiskBudgetService()
    now = datetime(2024, 1, 1, tzinfo=UTC)

    decision = svc.decide(
        settings=settings,
        now_utc=now,
        inputs=Stage7RiskInputs(**{**_inputs().__dict__, "max_drawdown_pct": Decimal("0.25")}),
        previous_decision=None,
    )

    assert decision.mode == RiskMode.OBSERVE_ONLY
    assert decision.cooldown_until == now + timedelta(seconds=settings.stage7_risk_cooldown_sec)


def test_spread_spike_sets_reduce_risk_only() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    svc = Stage7RiskBudgetService()

    decision = svc.decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=Stage7RiskInputs(**{**_inputs().__dict__, "observed_spread_bps": Decimal("999")}),
        previous_decision=None,
    )

    assert decision.mode == RiskMode.REDUCE_RISK_ONLY


def test_cooldown_monotonicity_does_not_upgrade() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    svc = Stage7RiskBudgetService()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    previous = RiskDecision(
        mode=RiskMode.OBSERVE_ONLY,
        reasons={"rules": [{"code": "prior"}]},
        cooldown_until=now + timedelta(minutes=10),
        decided_at=now - timedelta(minutes=1),
        inputs_hash="prev",
    )

    decision = svc.decide(
        settings=settings, now_utc=now, inputs=_inputs(), previous_decision=previous
    )

    assert decision.mode == RiskMode.OBSERVE_ONLY
    assert decision.cooldown_until == previous.cooldown_until


def test_stale_data_sets_observe_only() -> None:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    svc = Stage7RiskBudgetService()

    decision = svc.decide(
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        inputs=Stage7RiskInputs(**{**_inputs().__dict__, "market_data_age_sec": 10_000}),
        previous_decision=None,
    )

    assert decision.mode == RiskMode.OBSERVE_ONLY
