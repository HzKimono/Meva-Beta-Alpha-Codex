from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.adaptation_models import Stage7Params
from btcbot.services.adaptation_service import AdaptationService


def _active() -> Stage7Params:
    return Stage7Params(
        universe_size=20,
        score_weights={
            "liquidity": Decimal("0.5"),
            "spread": Decimal("0.3"),
            "volatility": Decimal("0.2"),
        },
        order_offset_bps=10,
        turnover_cap_try=Decimal("1000"),
        max_orders_per_cycle=3,
        max_spread_bps=200,
        cash_target_try=Decimal("300"),
        min_quote_volume_try=Decimal("0"),
        version=1,
        updated_at=datetime.now(UTC),
    )


def test_proposal_reject_spike_reduces_offset() -> None:
    svc = AdaptationService()
    settings = Settings()
    candidate, change = svc.propose_update(
        recent_metrics=[
            {
                "alert_flags": {"reject_spike": True},
                "quality_flags": {},
                "max_drawdown_pct": "0",
                "mode_final": "NORMAL",
            }
        ],
        active_params=_active(),
        settings=settings,
    )
    assert candidate.order_offset_bps == 8
    assert "order_offset_bps" in change.changes


def test_proposal_healthy_window_increases_universe_and_turnover() -> None:
    svc = AdaptationService()
    settings = Settings()
    healthy = [
        {
            "alert_flags": {},
            "quality_flags": {},
            "max_drawdown_pct": "0",
            "mode_final": "NORMAL",
            "net_pnl_try": "10",
            "oms_rejected_count": 0,
            "oms_throttled_count": 0,
        }
    ] * 3
    candidate, change = svc.propose_update(
        recent_metrics=healthy,
        active_params=_active(),
        settings=settings,
    )
    assert candidate.universe_size == 21
    assert candidate.turnover_cap_try == Decimal("1000")
    assert "universe_size" in change.changes
