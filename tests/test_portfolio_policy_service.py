from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import Balance
from btcbot.domain.risk_budget import Mode
from btcbot.services.portfolio_policy_service import PortfolioPolicyService, split_symbol


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DRY_RUN": True,
        "STAGE7_ENABLED": True,
        "SYMBOLS": "BTC_TRY,ETH_TRY,XRP_TRY",
        "TRY_CASH_TARGET": "300",
        "TRY_CASH_MAX": "600",
        "MAX_POSITION_NOTIONAL_TRY": "10000",
        "NOTIONAL_CAP_TRY_PER_CYCLE": "100000",
        "MAX_ORDERS_PER_CYCLE": 10,
        "MIN_ORDER_NOTIONAL_TRY": 10,
        "STAGE7_UNIVERSE_QUOTE_CCY": "TRY",
    }
    base.update(overrides)
    return Settings(**base)


def test_split_symbol_parses_canonical_with_quote_suffix() -> None:
    assert split_symbol("BTCTRY", "TRY") == ("BTC", "TRY")


def test_split_symbol_parses_underscore() -> None:
    assert split_symbol("BTC_TRY", "TRY") == ("BTC", "TRY")


def test_split_symbol_parses_dash() -> None:
    assert split_symbol("BTC-TRY", "TRY") == ("BTC", "TRY")


def test_split_symbol_non_parseable_has_fallback() -> None:
    assert split_symbol("SOMETHING", "TRY") == ("SOMETHING", "")


def test_equal_weight_allocation_sums_to_one_and_is_deterministic() -> None:
    service = PortfolioPolicyService()
    settings = _settings(TRY_CASH_TARGET="0", TRY_CASH_MAX="0")
    now = datetime(2024, 1, 1, tzinfo=UTC)
    balances = [Balance(asset="TRY", free=900)]
    prices = {"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100"), "XRPTRY": Decimal("100")}

    first = service.build_plan(
        universe=["ETH_TRY", "BTC_TRY", "XRP_TRY"],
        mark_prices_try=prices,
        balances=balances,
        settings=settings,
        now_utc=now,
    )
    second = service.build_plan(
        universe=["ETH_TRY", "BTC_TRY", "XRP_TRY"],
        mark_prices_try=prices,
        balances=balances,
        settings=settings,
        now_utc=now,
    )

    total_weight = sum((allocation.weight for allocation in first.allocations), start=Decimal("0"))
    assert (Decimal("1") - total_weight).copy_abs() <= Decimal("0.0000000001")
    assert [allocation.to_dict() for allocation in first.allocations] == [
        allocation.to_dict() for allocation in second.allocations
    ]
    assert [action.to_dict() for action in first.actions] == [
        action.to_dict() for action in second.actions
    ]


def test_cash_buffer_respected_when_equity_small_allocations_shrink() -> None:
    service = PortfolioPolicyService()
    settings = _settings(TRY_CASH_TARGET="300", TRY_CASH_MAX="600")
    plan = service.build_plan(
        universe=["BTC_TRY", "ETH_TRY"],
        mark_prices_try={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100")},
        balances=[Balance(asset="TRY", free=200)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert plan.cash_target_try == Decimal("300")
    assert plan.allocations == []
    assert plan.actions == []


def test_per_symbol_cap_enforced() -> None:
    service = PortfolioPolicyService()
    settings = _settings(
        TRY_CASH_TARGET="0",
        TRY_CASH_MAX="0",
        MAX_POSITION_NOTIONAL_TRY="100",
        NOTIONAL_CAP_TRY_PER_CYCLE="100000",
    )
    plan = service.build_plan(
        universe=["BTC_TRY", "ETH_TRY"],
        mark_prices_try={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("50")},
        balances=[Balance(asset="TRY", free=1000)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert all(allocation.target_notional_try <= Decimal("100") for allocation in plan.allocations)
    assert any("per_symbol_cap_hits=2" in note for note in plan.notes)


def test_turnover_cap_and_max_orders_enforced_sell_first() -> None:
    service = PortfolioPolicyService()
    settings = _settings(
        TRY_CASH_TARGET="0",
        TRY_CASH_MAX="0",
        NOTIONAL_CAP_TRY_PER_CYCLE="300",
        MAX_ORDERS_PER_CYCLE=1,
        MIN_ORDER_NOTIONAL_TRY=10,
    )
    plan = service.build_plan(
        universe=["BTC_TRY", "ETH_TRY", "XRP_TRY"],
        mark_prices_try={
            "BTCTRY": Decimal("100"),
            "ETHTRY": Decimal("100"),
            "XRPTRY": Decimal("100"),
        },
        balances=[Balance(asset="BTC", free=8), Balance(asset="TRY", free=300)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].side == "SELL"
    assert plan.actions[0].target_notional_try <= Decimal("300")
    assert any(
        note.startswith("turnover_cap=") or note.startswith("max_orders=") for note in plan.notes
    )


def test_reduce_risk_only_results_in_sell_only_actions() -> None:
    service = PortfolioPolicyService()
    settings = _settings(TRY_CASH_TARGET="0", TRY_CASH_MAX="0")
    plan = service.build_plan(
        universe=["BTC_TRY", "ETH_TRY"],
        mark_prices_try={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100")},
        balances=[Balance(asset="BTC", free=8), Balance(asset="TRY", free=0)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        final_mode=Mode.REDUCE_RISK_ONLY,
    )

    assert all(action.side == "SELL" for action in plan.actions)


def test_observe_only_results_in_empty_actions() -> None:
    service = PortfolioPolicyService()
    settings = _settings(TRY_CASH_TARGET="0", TRY_CASH_MAX="0")
    plan = service.build_plan(
        universe=["BTC_TRY", "ETH_TRY"],
        mark_prices_try={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100")},
        balances=[Balance(asset="BTC", free=8), Balance(asset="TRY", free=0)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
        final_mode=Mode.OBSERVE_ONLY,
    )

    assert plan.actions == []
    assert any(note.startswith("observe_only=") for note in plan.notes)


def test_decimal_mixed_settings_types_are_normalized() -> None:
    service = PortfolioPolicyService()
    settings = _settings(
        TRY_CASH_TARGET=300,
        TRY_CASH_MAX=600,
        MAX_POSITION_NOTIONAL_TRY=5000,
        NOTIONAL_CAP_TRY_PER_CYCLE=1000,
        MIN_ORDER_NOTIONAL_TRY=10.0,
    )

    plan = service.build_plan(
        universe=["BTC_TRY"],
        mark_prices_try={"BTCTRY": Decimal("100")},
        balances=[Balance(asset="TRY", free=1000)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert plan.cash_target_try == Decimal("300")
    assert plan.allocations


def test_sell_priority_applies_during_turnover_allocation_even_if_buy_is_larger() -> None:
    service = PortfolioPolicyService()
    settings = _settings(
        TRY_CASH_TARGET="0",
        TRY_CASH_MAX="0",
        NOTIONAL_CAP_TRY_PER_CYCLE="300",
        MAX_ORDERS_PER_CYCLE=1,
        MIN_ORDER_NOTIONAL_TRY=10,
    )

    plan = service.build_plan(
        universe=["BTC_TRY", "ETH_TRY"],
        mark_prices_try={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100")},
        balances=[Balance(asset="BTC", free=6), Balance(asset="TRY", free=200)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].side == "SELL"


def test_sell_priority_applies_during_max_orders_selection_even_if_buy_is_larger() -> None:
    service = PortfolioPolicyService()
    settings = _settings(
        TRY_CASH_TARGET="0",
        TRY_CASH_MAX="0",
        NOTIONAL_CAP_TRY_PER_CYCLE="1000",
        MAX_ORDERS_PER_CYCLE=1,
        MIN_ORDER_NOTIONAL_TRY=10,
    )

    plan = service.build_plan(
        universe=["BTC_TRY", "ETH_TRY"],
        mark_prices_try={"BTCTRY": Decimal("100"), "ETHTRY": Decimal("100")},
        balances=[Balance(asset="BTC", free=6), Balance(asset="TRY", free=200)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].side == "SELL"


def test_weighted_allocations_follow_portfolio_targets() -> None:
    service = PortfolioPolicyService()
    settings = _settings(
        TRY_CASH_TARGET="0",
        TRY_CASH_MAX="0",
        SYMBOLS="BTCTRY,ETHTRY,SOLTRY,AVAXTRY,ADATRY",
        PORTFOLIO_TARGETS="BTCTRY:0.4,ETHTRY:0.2,SOLTRY:0.2,AVAXTRY:0.1,ADATRY:0.1",
    )
    plan = service.build_plan(
        universe=["BTCTRY", "ETHTRY", "SOLTRY", "AVAXTRY", "ADATRY"],
        mark_prices_try={
            "BTCTRY": Decimal("100"),
            "ETHTRY": Decimal("100"),
            "SOLTRY": Decimal("100"),
            "AVAXTRY": Decimal("100"),
            "ADATRY": Decimal("100"),
        },
        balances=[Balance(asset="TRY", free=1000)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    by_symbol = {allocation.symbol: allocation.target_notional_try for allocation in plan.allocations}
    assert by_symbol["BTCTRY"] == Decimal("400")
    assert by_symbol["ETHTRY"] == Decimal("200")
    assert by_symbol["SOLTRY"] == Decimal("200")
    assert by_symbol["AVAXTRY"] == Decimal("100")
    assert by_symbol["ADATRY"] == Decimal("100")


def test_weighted_allocations_respect_max_position_cap() -> None:
    service = PortfolioPolicyService()
    settings = _settings(
        TRY_CASH_TARGET="0",
        TRY_CASH_MAX="0",
        SYMBOLS="BTCTRY,ETHTRY,SOLTRY,AVAXTRY,ADATRY",
        PORTFOLIO_TARGETS="BTCTRY:0.8,ETHTRY:0.1,SOLTRY:0.05,AVAXTRY:0.03,ADATRY:0.02",
        MAX_POSITION_NOTIONAL_TRY="500",
    )
    plan = service.build_plan(
        universe=["BTCTRY", "ETHTRY", "SOLTRY", "AVAXTRY", "ADATRY"],
        mark_prices_try={
            "BTCTRY": Decimal("100"),
            "ETHTRY": Decimal("100"),
            "SOLTRY": Decimal("100"),
            "AVAXTRY": Decimal("100"),
            "ADATRY": Decimal("100"),
        },
        balances=[Balance(asset="TRY", free=1000)],
        settings=settings,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    by_symbol = {allocation.symbol: allocation.target_notional_try for allocation in plan.allocations}
    assert by_symbol["BTCTRY"] == Decimal("500")
    assert any(note.startswith("leftover_weight_to_cash=") for note in plan.notes)
