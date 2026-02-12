from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.action_to_order import (
    build_deterministic_client_order_id,
    sized_action_to_order,
)
from btcbot.domain.allocation import SizedAction


def test_sized_action_to_order_maps_stage4_fields() -> None:
    action = SizedAction(
        symbol="BTC_TRY",
        side="buy",
        notional_try=Decimal("25"),
        qty=Decimal("0.25"),
        rationale="allocation:ok",
        strategy_id="baseline_mean_reversion_v1",
        intent_index=3,
    )
    created_at = datetime(2024, 1, 1, tzinfo=UTC)

    order = sized_action_to_order(action, mode="dry_run", created_at=created_at)

    assert order.symbol == "BTCTRY"
    assert order.side == "buy"
    assert order.qty == Decimal("0.25")
    assert order.price == Decimal("100")
    assert order.type == "limit"
    assert order.client_order_id == build_deterministic_client_order_id(action)
    assert order.mode == "dry_run"
    assert order.created_at == created_at
    assert order.updated_at == created_at


def test_client_order_id_is_deterministic() -> None:
    action = SizedAction(
        symbol="ETH_TRY",
        side="sell",
        notional_try=Decimal("10"),
        qty=Decimal("0.1"),
        rationale="allocation:scaled",
        strategy_id="alpha",
        intent_index=1,
    )

    first = build_deterministic_client_order_id(action)
    second = build_deterministic_client_order_id(action)

    assert first == second
    assert "s:alpha" in first
    assert "side:sell" in first
