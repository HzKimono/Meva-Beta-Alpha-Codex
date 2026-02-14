from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.adaptation_models import Stage7Params
from btcbot.services.param_bounds import ParamBounds


def test_bounds_enforced_and_weights_normalized_deterministically() -> None:
    settings = Settings()
    params = Stage7Params(
        universe_size=100,
        score_weights={
            "liquidity": Decimal("2"),
            "spread": Decimal("-1"),
            "volatility": Decimal("1"),
        },
        order_offset_bps=99,
        turnover_cap_try=Decimal("999999"),
        max_orders_per_cycle=99,
        max_spread_bps=999,
        cash_target_try=Decimal("999999"),
        min_quote_volume_try=Decimal("-1"),
        version=1,
        updated_at=datetime.now(UTC),
    )

    bounded = ParamBounds.apply_bounds(params, settings)
    assert bounded.universe_size == 50
    assert bounded.order_offset_bps == 50
    assert bounded.max_orders_per_cycle == 20
    assert bounded.max_spread_bps == 500
    assert bounded.min_quote_volume_try == Decimal("0")
    assert bounded.cash_target_try == settings.try_cash_max
    assert bounded.turnover_cap_try == settings.notional_cap_try_per_cycle
    assert sum(bounded.score_weights.values()) == Decimal("1")
    assert bounded.score_weights["spread"] == Decimal("0")
    assert bounded.score_weights["liquidity"] == Decimal("0.5")
    assert bounded.score_weights["volatility"] == Decimal("0.5")


def test_normalize_weights_zeroes_fallback_defaults() -> None:
    normalized = ParamBounds.normalize_weights(
        {
            "liquidity": Decimal("0"),
            "spread": Decimal("0"),
            "volatility": Decimal("0"),
        }
    )
    assert normalized == {
        "liquidity": Decimal("0.5"),
        "spread": Decimal("0.3"),
        "volatility": Decimal("0.2"),
    }
