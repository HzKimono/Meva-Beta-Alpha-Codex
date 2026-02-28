from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.risk_budget import Mode, RiskLimits
from btcbot.domain.stage4 import PnLSnapshot
from btcbot.services.ledger_service import PnlReport
from btcbot.services.risk_budget_service import RiskBudgetService
from btcbot.services.state_store import StateStore


def _limits() -> RiskLimits:
    return RiskLimits(
        max_daily_drawdown_try=Decimal("1000"),
        max_drawdown_try=Decimal("1000"),
        max_gross_exposure_try=Decimal("5000"),
        max_position_pct=Decimal("0.9"),
        max_order_notional_try=Decimal("1000"),
        max_fee_try_per_day=Decimal("100"),
    )


def _snapshot(idx: int, realized_today: Decimal, equity: Decimal) -> PnLSnapshot:
    return PnLSnapshot(
        total_equity_try=equity,
        realized_today_try=realized_today,
        realized_total_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=idx),
    )


def test_compute_consecutive_loss_streak_cases() -> None:
    seq = [
        _snapshot(3, Decimal("-10"), Decimal("100")),
        _snapshot(2, Decimal("-5"), Decimal("100")),
        _snapshot(1, Decimal("0"), Decimal("100")),
        _snapshot(0, Decimal("-3"), Decimal("100")),
    ]
    assert RiskBudgetService.compute_consecutive_loss_streak(seq, lookback=10) == 2

    seq_win_newest = [
        _snapshot(2, Decimal("5"), Decimal("100")),
        _snapshot(1, Decimal("-5"), Decimal("100")),
        _snapshot(0, Decimal("-2"), Decimal("100")),
    ]
    assert RiskBudgetService.compute_consecutive_loss_streak(seq_win_newest, lookback=10) == 0

    seq_lookback = [
        _snapshot(2, Decimal("-1"), Decimal("100")),
        _snapshot(1, Decimal("-2"), Decimal("100")),
        _snapshot(0, Decimal("-3"), Decimal("100")),
    ]
    assert RiskBudgetService.compute_consecutive_loss_streak(seq_lookback, lookback=2) == 2


def test_compute_volatility_regime_cases() -> None:
    low, _ = RiskBudgetService.compute_volatility_regime(
        [_snapshot(i, Decimal("0"), Decimal("1000")) for i in range(10)][::-1],
        lookback=10,
        low_threshold=Decimal("0.001"),
        high_threshold=Decimal("0.01"),
    )
    assert low == "low"

    high_series = [
        _snapshot(i, Decimal("0"), Decimal(str(v)))
        for i, v in enumerate([1000, 1200, 900, 1300, 800, 1400, 750, 1450])
    ][::-1]
    high, _ = RiskBudgetService.compute_volatility_regime(
        high_series,
        lookback=8,
        low_threshold=Decimal("0.001"),
        high_threshold=Decimal("0.05"),
    )
    assert high == "high"

    normal, _ = RiskBudgetService.compute_volatility_regime(
        [_snapshot(i, Decimal("0"), Decimal("1000")) for i in range(3)][::-1],
        lookback=3,
        low_threshold=Decimal("0.001"),
        high_threshold=Decimal("0.01"),
    )
    assert normal == "normal"


def test_compute_decision_uses_computed_signal_inputs(tmp_path) -> None:
    db = StateStore(str(tmp_path / "risk_signal.sqlite"))
    now = datetime(2026, 2, 1, 12, tzinfo=UTC)
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_MAX_CONSECUTIVE_LOSSES=3,
        STAGE7_VOL_LOOKBACK=8,
        STAGE7_VOL_LOW_THRESHOLD="0.001",
        STAGE7_VOL_HIGH_THRESHOLD="0.05",
    )
    service = RiskBudgetService(db, settings=settings, now_provider=lambda: now)

    # newest snapshots (DESC by ts): three losses and high-vol equity path
    equities = [1000, 1200, 900, 1300, 800, 1400, 750, 1450]
    for idx, eq in enumerate(equities):
        db.save_stage4_pnl_snapshot(
            PnLSnapshot(
                total_equity_try=Decimal(str(eq)),
                realized_today_try=Decimal("-10") if idx >= 5 else Decimal("5"),
                realized_total_try=Decimal("0"),
                drawdown_pct=Decimal("0"),
                ts=now - timedelta(days=(len(equities) - idx)),
            )
        )

    decision, *_ = service.compute_decision(
        cycle_id="test-cycle",
        limits=_limits(),
        pnl_report=PnlReport(
            realized_pnl_total=Decimal("0"),
            unrealized_pnl_total=Decimal("0"),
            fees_total_by_currency={"TRY": Decimal("0")},
            per_symbol=[],
            equity_estimate=Decimal("1000"),
        ),
        positions=[],
        mark_prices={},
        realized_today_try=Decimal("0"),
        kill_switch_active=False,
    )

    assert decision.budget_view.position_sizing_multiplier == Decimal("0.25")
    assert decision.budget_view.mode == Mode.NORMAL
