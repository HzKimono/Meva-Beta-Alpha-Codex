from __future__ import annotations

from decimal import Decimal

from btcbot.domain.strategy_core import Intent, PositionSummary
from btcbot.services.allocation_service import AllocationKnobs, AllocationService


def _intent(
    *,
    symbol: str = "BTC_TRY",
    side: str = "buy",
    intent_type: str = "place",
    target_notional_try: Decimal | None = Decimal("100"),
    strategy_id: str = "s1",
) -> Intent:
    return Intent(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        intent_type=intent_type,  # type: ignore[arg-type]
        target_notional_try=target_notional_try,
        rationale="unit-test",
        strategy_id=strategy_id,
    )


def test_buy_respects_cash_target_and_rejects_when_try_insufficient() -> None:
    knobs = AllocationKnobs(target_try_cash=Decimal("300"), min_order_notional_try=Decimal("10"))

    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("50"))],
        balances={"TRY": Decimal("305")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert len(result.actions) == 0
    assert result.decisions[0].status == "rejected"
    assert result.decisions[0].reason == "cash_target"


def test_buy_rejects_below_min_notional() -> None:
    knobs = AllocationKnobs(target_try_cash=Decimal("300"), min_order_notional_try=Decimal("10"))

    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("9"))],
        balances={"TRY": Decimal("500")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert len(result.actions) == 0
    assert result.decisions[0].reason == "min_notional"


def test_buy_respects_max_position_try_per_symbol() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("0"),
        min_order_notional_try=Decimal("10"),
        max_position_try_per_symbol=Decimal("120"),
    )

    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("50"))],
        balances={"TRY": Decimal("1000")},
        positions={
            "BTCTRY": PositionSummary(symbol="BTC_TRY", qty=Decimal("1"), avg_cost=Decimal("90"))
        },
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert len(result.actions) == 1
    assert result.actions[0].notional_try == Decimal("20")
    assert result.decisions[0].status == "scaled"


def test_sell_works_with_zero_try_balance() -> None:
    knobs = AllocationKnobs(target_try_cash=Decimal("300"), min_order_notional_try=Decimal("10"))

    result = AllocationService.allocate(
        intents=[_intent(side="sell", target_notional_try=Decimal("50"))],
        balances={"TRY": Decimal("0")},
        positions={
            "BTCTRY": PositionSummary(symbol="BTCTRY", qty=Decimal("1"), avg_cost=Decimal("80"))
        },
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert len(result.actions) == 1
    assert result.actions[0].notional_try == Decimal("50")
    assert result.decisions[0].status == "accepted"


def test_sell_rejects_when_no_position() -> None:
    knobs = AllocationKnobs(target_try_cash=Decimal("0"), min_order_notional_try=Decimal("10"))

    result = AllocationService.allocate(
        intents=[_intent(side="sell", target_notional_try=Decimal("20"))],
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert len(result.actions) == 0
    assert result.decisions[0].reason == "no_position"


def test_sell_notional_capped_by_position_value() -> None:
    knobs = AllocationKnobs(target_try_cash=Decimal("0"), min_order_notional_try=Decimal("10"))

    result = AllocationService.allocate(
        intents=[_intent(side="sell", target_notional_try=Decimal("500"))],
        balances={"TRY": Decimal("1000")},
        positions={
            "BTCTRY": PositionSummary(symbol="BTCTRY", qty=Decimal("1.2"), avg_cost=Decimal("80"))
        },
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert len(result.actions) == 1
    assert result.actions[0].notional_try == Decimal("120")
    assert result.decisions[0].status == "scaled"


def test_determinism_same_inputs_same_outputs() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("100"),
        min_order_notional_try=Decimal("10"),
        max_total_notional_try_per_cycle=Decimal("90"),
    )
    intents = [
        _intent(symbol="BTC_TRY", side="buy", target_notional_try=Decimal("80"), strategy_id="a"),
        _intent(symbol="ETH_TRY", side="buy", target_notional_try=Decimal("80"), strategy_id="b"),
    ]
    balances = {"TRY": Decimal("250")}
    positions = {}
    mark_prices = {"BTCTRY": Decimal("100"), "ETHTRY": Decimal("200")}

    first = AllocationService.allocate(
        intents=intents,
        balances=balances,
        positions=positions,
        mark_prices=mark_prices,
        knobs=knobs,
    )
    second = AllocationService.allocate(
        intents=intents,
        balances=balances,
        positions=positions,
        mark_prices=mark_prices,
        knobs=knobs,
    )

    assert first == second


def test_counters_reflect_reasons_and_accepts() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("300"),
        min_order_notional_try=Decimal("10"),
        max_position_try_per_symbol=Decimal("120"),
    )

    result = AllocationService.allocate(
        intents=[
            _intent(
                symbol="BTC_TRY", side="buy", target_notional_try=Decimal("30"), strategy_id="buy1"
            ),
            _intent(
                symbol="BTC_TRY", side="buy", target_notional_try=Decimal("9"), strategy_id="buy2"
            ),
            _intent(
                symbol="ETH_TRY",
                side="sell",
                target_notional_try=Decimal("20"),
                strategy_id="sell1",
            ),
            _intent(
                symbol="ETH_TRY",
                side="buy",
                intent_type="cancel",
                target_notional_try=None,
                strategy_id="cancel1",
            ),
        ],
        balances={"TRY": Decimal("500")},
        positions={
            "BTCTRY": PositionSummary(symbol="BTCTRY", qty=Decimal("1"), avg_cost=Decimal("90"))
        },
        mark_prices={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("200")},
        knobs=knobs,
    )

    assert result.counters["scaled"] == 1
    assert result.counters["rejected_min_notional"] == 1
    assert result.counters["rejected_no_position"] == 1
    assert result.counters["rejected_not_implemented_in_pr3"] == 1
