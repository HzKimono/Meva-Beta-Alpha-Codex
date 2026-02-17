from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.stage4 import Order, Position
from btcbot.services.decision_pipeline_service import DecisionPipelineService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.stage4_planning_kernel_integration import (
    Stage4CompatOrderIntentBuilderAdapter,
    build_stage4_kernel_plan,
    build_stage4_planning_context,
    normalize_stage4_open_orders,
)


def _pair(symbol: str) -> PairInfo:
    return PairInfo(
        pairSymbol=symbol,
        numeratorScale=6,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
        tickSize=Decimal("0.1"),
        stepSize=Decimal("0.0001"),
    )


def _canonical(intents: list[OrderIntent]) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for item in intents:
        rows.append(
            (
                item.client_order_id,
                item.symbol,
                item.side,
                item.order_type,
                str(item.price_try),
                str(item.qty),
                str(item.notional_try),
                item.reason,
                str(item.skipped),
                str(item.skip_reason),
                repr(sorted(item.constraints_applied.items())),
            )
        )
    return sorted(rows)


def _legacy_stage4_order_intents(
    *,
    settings: Settings,
    cycle_id: str,
    mark_prices: dict[str, Decimal],
    try_cash: Decimal,
    positions: list[Position],
    open_orders: list[Order],
    pair_info: list[PairInfo],
    preferred_symbols: list[str],
) -> list[OrderIntent]:
    runner = Stage4CycleRunner()
    decision = DecisionPipelineService(settings=settings).run_cycle(
        cycle_id=cycle_id,
        balances={"TRY": try_cash},
        positions={
            symbol: runner._to_position_summary(pos)
            for symbol, pos in {p.symbol: p for p in positions}.items()
        },
        mark_prices=mark_prices,
        open_orders=open_orders,
        pair_info=pair_info,
        bootstrap_enabled=settings.stage4_bootstrap_intents,
        live_mode=False,
        preferred_symbols=preferred_symbols,
        aggressive_scores=None,
    )
    pipeline_orders = list(decision.order_requests)
    selected_orders = pipeline_orders
    if not selected_orders:
        selected_orders, _ = runner._build_intents(
            cycle_id=cycle_id,
            symbols=preferred_symbols,
            mark_prices=mark_prices,
            try_cash=try_cash,
            open_orders=open_orders,
            live_mode=False,
            bootstrap_enabled=settings.stage4_bootstrap_intents,
            pair_info=pair_info,
        )

    as_intents: list[OrderIntent] = []
    for order in selected_orders:
        as_intents.append(
            OrderIntent(
                cycle_id=cycle_id,
                symbol=order.symbol,
                side=str(order.side).upper(),  # type: ignore[arg-type]
                order_type=str(order.type).upper(),  # type: ignore[arg-type]
                price_try=order.price,
                qty=order.qty,
                notional_try=order.price * order.qty,
                client_order_id=str(order.client_order_id or ""),
                reason=str(order.status),
                constraints_applied={},
            )
        )
    return as_intents


def test_stage4_kernel_planning_matches_legacy_order_intents_for_frozen_inputs() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    settings = Settings(DRY_RUN=True, STAGE4_USE_PLANNING_KERNEL=True, SYMBOLS="BTC_TRY,ETH_TRY")
    pair_info = [_pair("BTCTRY"), _pair("ETHTRY")]
    positions = [
        Position(
            symbol="BTCTRY",
            qty=Decimal("0.10"),
            avg_cost_try=Decimal("900"),
            realized_pnl_try=Decimal("0"),
            last_update_ts=now,
        )
    ]
    open_orders: list[Order] = []
    mark_prices = {"BTCTRY": Decimal("1000"), "ETHTRY": Decimal("100")}
    preferred = ["BTCTRY", "ETHTRY"]

    kernel = build_stage4_kernel_plan(
        settings=settings,
        cycle_id="c1",
        now_utc=now,
        selected_symbols=preferred,
        mark_prices=mark_prices,
        try_cash=Decimal("10000"),
        positions=positions,
        open_orders=open_orders,
        pair_info=pair_info,
        live_mode=False,
        aggressive_scores=None,
        bootstrap_builder=Stage4CycleRunner()._build_intents,
    )
    legacy = _legacy_stage4_order_intents(
        settings=settings,
        cycle_id="c1",
        mark_prices=mark_prices,
        try_cash=Decimal("10000"),
        positions=positions,
        open_orders=open_orders,
        pair_info=pair_info,
        preferred_symbols=preferred,
    )

    assert _canonical(list(kernel.plan.order_intents)) == _canonical(legacy)


def test_stage4_kernel_planning_matches_legacy_with_open_orders() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    settings = Settings(DRY_RUN=True, STAGE4_USE_PLANNING_KERNEL=True, SYMBOLS="BTC_TRY,ETH_TRY")
    pair_info = [_pair("BTCTRY"), _pair("ETHTRY")]
    open_orders = [
        Order(
            symbol="BTCTRY",
            side="buy",
            type="limit",
            price=Decimal("1000"),
            qty=Decimal("0.01"),
            status="new",
            created_at=now,
            updated_at=now,
            client_order_id="open-1",
        )
    ]
    mark_prices = {"BTCTRY": Decimal("1000"), "ETHTRY": Decimal("100")}
    preferred = ["BTCTRY", "ETHTRY"]

    kernel = build_stage4_kernel_plan(
        settings=settings,
        cycle_id="c2",
        now_utc=now,
        selected_symbols=preferred,
        mark_prices=mark_prices,
        try_cash=Decimal("10000"),
        positions=[],
        open_orders=open_orders,
        pair_info=pair_info,
        live_mode=False,
        aggressive_scores=None,
        bootstrap_builder=Stage4CycleRunner()._build_intents,
    )
    legacy = _legacy_stage4_order_intents(
        settings=settings,
        cycle_id="c2",
        mark_prices=mark_prices,
        try_cash=Decimal("10000"),
        positions=[],
        open_orders=open_orders,
        pair_info=pair_info,
        preferred_symbols=preferred,
    )

    assert _canonical(list(kernel.plan.order_intents)) == _canonical(legacy)


def test_stage4_normalization_is_strict_for_open_orders_and_order_intents() -> None:
    bad_order = Order(
        symbol="BTCTRY",
        side="weird",
        type="broken",
        price=Decimal("100"),
        qty=Decimal("1"),
        status="new",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        client_order_id="cid-1",
    )

    with pytest.raises(ValueError):
        normalize_stage4_open_orders([bad_order], on_invalid="raise")

    assert normalize_stage4_open_orders([bad_order], on_invalid="skip") == []

    class _FakeStrategy:
        @property
        def last_decision_report(self):
            return type("R", (), {"order_requests": [bad_order]})()

    adapter = Stage4CompatOrderIntentBuilderAdapter(
        strategy_adapter=_FakeStrategy(),  # type: ignore[arg-type]
        bootstrap_builder=Stage4CycleRunner()._build_intents,
        live_mode=False,
    )
    context = build_stage4_planning_context(
        cycle_id="cx",
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        selected_symbols=["BTCTRY"],
        mark_prices={"BTCTRY": Decimal("100")},
        try_cash=Decimal("100"),
        positions=[],
        open_orders=[],
        pair_info=[_pair("BTCTRY")],
        live_mode=False,
        bootstrap_enabled=True,
        aggressive_scores=None,
    )

    intents = adapter.build(context, [])
    assert len(intents) == 1
    assert intents[0].skipped is True
    assert intents[0].skip_reason == "invalid_normalized_fields"
