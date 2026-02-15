from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

import pytest

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


def test_buy_scaled_reason_uses_binding_constraint_with_precedence() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("180"),
        min_order_notional_try=Decimal("10"),
        max_intent_notional_try=Decimal("90"),
        max_position_try_per_symbol=Decimal("150"),
        max_total_notional_try_per_cycle=Decimal("70"),
    )

    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("100"))],
        balances={"TRY": Decimal("200")},
        positions={
            "BTCTRY": PositionSummary(symbol="BTCTRY", qty=Decimal("1"), avg_cost=Decimal("80"))
        },
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert result.decisions[0].status == "scaled"
    assert result.decisions[0].reason == "cash_target"
    assert result.actions[0].rationale == "allocation:cash_target"


def test_buy_scaled_reason_max_position_exposure_cap() -> None:
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
    assert result.decisions[0].reason == "max_position_exposure_cap"


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
    assert result.decisions[0].reason == "ok"


def test_sell_rejects_when_no_position_before_mark_price_check() -> None:
    knobs = AllocationKnobs(target_try_cash=Decimal("0"), min_order_notional_try=Decimal("10"))

    result = AllocationService.allocate(
        intents=[_intent(side="sell", target_notional_try=Decimal("20"))],
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={},
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
    assert result.decisions[0].reason == "position_value_cap"


def test_cancel_replace_rejected_with_explicit_reason() -> None:
    result = AllocationService.allocate(
        intents=[_intent(intent_type="cancel", target_notional_try=None)],
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=AllocationKnobs(),
    )

    assert result.decisions[0].reason == "not_implemented_in_pr3"


def test_traceability_intent_index_exists_on_decisions_and_actions() -> None:
    result = AllocationService.allocate(
        intents=[
            _intent(
                symbol="BTC_TRY", side="buy", target_notional_try=Decimal("10"), strategy_id="a"
            ),
            _intent(
                symbol="ETH_TRY", side="sell", target_notional_try=Decimal("20"), strategy_id="b"
            ),
        ],
        balances={"TRY": Decimal("1000")},
        positions={
            "ETHTRY": PositionSummary(symbol="ETHTRY", qty=Decimal("1"), avg_cost=Decimal("100"))
        },
        mark_prices={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("200")},
        knobs=AllocationKnobs(target_try_cash=Decimal("0")),
    )

    assert [decision.intent_index for decision in result.decisions] == [0, 1]
    assert [action.intent_index for action in result.actions] == [0, 1]


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


def test_determinism_ignores_mapping_insertion_order() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("100"), max_total_notional_try_per_cycle=Decimal("150")
    )
    intents = [
        _intent(symbol="BTC_TRY", side="buy", target_notional_try=Decimal("80"), strategy_id="a"),
        _intent(symbol="ETH_TRY", side="sell", target_notional_try=Decimal("50"), strategy_id="b"),
    ]

    result_a = AllocationService.allocate(
        intents=intents,
        balances={"TRY": Decimal("300"), "USDT": Decimal("1")},
        positions={
            "ETHTRY": PositionSummary(symbol="ETHTRY", qty=Decimal("1"), avg_cost=Decimal("100"))
        },
        mark_prices={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("200")},
        knobs=knobs,
    )
    result_b = AllocationService.allocate(
        intents=intents,
        balances={"USDT": Decimal("1"), "TRY": Decimal("300")},
        positions={
            "ETHTRY": PositionSummary(symbol="ETH_TRY", qty=Decimal("1"), avg_cost=Decimal("100"))
        },
        mark_prices={"ETHTRY": Decimal("200"), "BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert result_a == result_b


def test_immutable_allocation_result_boundary() -> None:
    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("20"))],
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=AllocationKnobs(target_try_cash=Decimal("0")),
    )

    assert isinstance(result.actions, tuple)
    assert isinstance(result.decisions, tuple)
    assert isinstance(result.counters, MappingProxyType)

    with pytest.raises(AttributeError):
        result.actions.append(result.actions[0])  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        result.counters["x"] = 1  # type: ignore[index]


def test_knobs_zero_caps_disable_limits() -> None:
    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("300"))],
        balances={"TRY": Decimal("500")},
        positions={
            "BTCTRY": PositionSummary(symbol="BTCTRY", qty=Decimal("5"), avg_cost=Decimal("1"))
        },
        mark_prices={"BTCTRY": Decimal("50")},
        knobs=AllocationKnobs(
            target_try_cash=Decimal("0"),
            min_order_notional_try=Decimal("10"),
            max_intent_notional_try=Decimal("0"),
            max_position_try_per_symbol=Decimal("0"),
            max_total_notional_try_per_cycle=Decimal("0"),
        ),
    )

    assert result.decisions[0].status == "accepted"
    assert result.decisions[0].reason == "ok"
    assert result.actions[0].notional_try == Decimal("300")


def test_counters_include_status_and_reason_breakdown() -> None:
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
    assert result.counters["rejected"] == 3
    assert result.counters["scaled_max_position_exposure_cap"] == 1
    assert result.counters["rejected_min_notional"] == 1
    assert result.counters["rejected_no_position"] == 1
    assert result.counters["rejected_not_implemented_in_pr3"] == 1


def test_small_try_balance_ceil_to_min_notional_when_caps_allow() -> None:
    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("20"))],
        balances={"TRY": Decimal("1712")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100000")},
        knobs=AllocationKnobs(
            target_try_cash=Decimal("0"),
            min_order_notional_try=Decimal("50"),
            max_total_notional_try_per_cycle=Decimal("100"),
            max_position_try_per_symbol=Decimal("100"),
        ),
    )

    assert len(result.actions) == 1
    assert result.actions[0].notional_try == Decimal("50")
    assert result.decisions[0].status in {"scaled", "accepted"}


def test_small_try_balance_rejects_with_blocking_cap_diagnostics() -> None:
    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("20"))],
        balances={"TRY": Decimal("1712")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100000")},
        knobs=AllocationKnobs(
            target_try_cash=Decimal("1700"),
            min_order_notional_try=Decimal("50"),
            max_total_notional_try_per_cycle=Decimal("30"),
            max_position_try_per_symbol=Decimal("100"),
        ),
    )

    assert len(result.actions) == 0
    assert result.decisions[0].reason in {"cash_target", "min_notional"}
    assert result.decisions[0].diagnostics is not None
    assert result.decisions[0].diagnostics["blocking_cap"] in {
        "cash_target",
        "cycle_notional_cap",
        "max_position_exposure_cap",
    }


def test_buy_respects_fee_buffer_remaining_cash_at_or_above_target() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("300"),
        min_order_notional_try=Decimal("10"),
        fee_buffer_bps=Decimal("100"),
    )

    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("100"))],
        balances={"TRY": Decimal("400")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )

    assert len(result.actions) == 1
    spent_with_fee = result.actions[0].notional_try * (
        Decimal("1") + knobs.fee_buffer_bps / Decimal("10000")
    )
    assert Decimal("400") - spent_with_fee >= Decimal("300")


def test_buy_respects_try_cash_max_cap() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("300"),
        try_cash_max=Decimal("350"),
        min_order_notional_try=Decimal("10"),
    )
    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("100"))],
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )
    assert len(result.actions) == 1
    assert result.actions[0].notional_try == Decimal("50")


def test_investable_usage_fraction_limits_budget() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("300"),
        investable_usage_mode="fraction",
        investable_usage_fraction=Decimal("0.5"),
    )
    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("500"))],
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )
    assert result.planned_total_try <= Decimal("350")
    assert result.unused_investable_try >= Decimal("300")


def test_investable_usage_cap_limits_budget() -> None:
    knobs = AllocationKnobs(
        target_try_cash=Decimal("300"),
        investable_usage_mode="cap",
        max_try_per_cycle=Decimal("120"),
    )
    result = AllocationService.allocate(
        intents=[_intent(target_notional_try=Decimal("500"))],
        balances={"TRY": Decimal("1000")},
        positions={},
        mark_prices={"BTCTRY": Decimal("100")},
        knobs=knobs,
    )
    assert result.planned_total_try <= Decimal("120")
    assert result.cash_try - result.planned_total_try >= result.cash_target_try
