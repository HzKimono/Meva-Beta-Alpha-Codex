from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.action_to_order import (
    build_deterministic_client_order_id,
    build_exchange_rules,
    sized_action_to_order,
)
from btcbot.domain.allocation import SizedAction
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import Quantizer


def test_sized_action_to_order_uses_quantizer_and_mark_price() -> None:
    action = SizedAction(
        symbol="BTC_TRY",
        side="buy",
        notional_try=Decimal("25"),
        qty=Decimal("0.1234567"),
        rationale="allocation:ok",
        strategy_id="baseline_mean_reversion_v1",
        intent_index=3,
    )
    pair = PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=4,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        status="TRADING",
    )
    created_at = datetime(2024, 1, 1, tzinfo=UTC)
    mark_price = Decimal("100.129")

    order, reason = sized_action_to_order(
        action,
        mode="dry_run",
        mark_price=mark_price,
        pair_info=pair,
        created_at=created_at,
    )

    assert reason is None
    assert order is not None
    rules = build_exchange_rules(pair)
    assert order.symbol == "BTCTRY"
    assert order.side == "buy"
    assert order.qty == Quantizer.quantize_qty(action.qty, rules)
    assert order.price == Quantizer.quantize_price(mark_price, rules)
    assert order.type == "limit"
    assert order.client_order_id == build_deterministic_client_order_id(action)
    assert order.mode == "dry_run"
    assert order.created_at == created_at
    assert order.updated_at == created_at


def test_sized_action_to_order_drops_min_notional_after_quantize() -> None:
    action = SizedAction(
        symbol="ETH_TRY",
        side="sell",
        notional_try=Decimal("10"),
        qty=Decimal("0.000011"),
        rationale="allocation:scaled",
        strategy_id="alpha",
        intent_index=1,
    )
    pair = PairInfo(
        pairSymbol="ETHTRY",
        numeratorScale=6,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        status="TRADING",
    )

    order, reason = sized_action_to_order(
        action,
        mode="dry_run",
        mark_price=Decimal("100"),
        pair_info=pair,
    )

    assert order is None
    assert reason == "dropped_min_notional_after_quantize"


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
    assert len(first) <= 64
    assert all(ch.isalnum() or ch in {":", "|", "-", "_"} for ch in first)
