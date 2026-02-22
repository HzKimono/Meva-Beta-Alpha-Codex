from __future__ import annotations

from decimal import Decimal

from btcbot.domain.models import Balance, SymbolInfo
from btcbot.services.state_store import StateStore
from btcbot.services.sweep_service import SweepService


def test_sweep_service_allocates_excess_and_leaves_target(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(
        state_store=store,
        target_try=300.0,
        offset_bps=20,
        default_min_notional=10.0,
    )

    balances = [Balance(asset="TRY", free=900.0)]
    bids = {"BTC_TRY": 100000.0, "ETH_TRY": 50000.0, "SOL_TRY": 1000.0}
    rules = {
        "BTC_TRY": SymbolInfo(
            symbol="BTC_TRY",
            base_asset="BTC",
            min_notional=10,
            step_size=0.000001,
            tick_size=0.1,
        ),
        "ETH_TRY": SymbolInfo(
            symbol="ETH_TRY",
            base_asset="ETH",
            min_notional=10,
            step_size=0.00001,
            tick_size=0.1,
        ),
        "SOL_TRY": SymbolInfo(
            symbol="SOL_TRY",
            base_asset="SOL",
            min_notional=10,
            step_size=0.001,
            tick_size=0.01,
        ),
    }

    intents = service.build_order_intents(
        cycle_id="202601010101",
        balances=balances,
        symbols=["BTC_TRY", "ETH_TRY", "SOL_TRY"],
        best_bids=bids,
        symbol_rules=rules,
    )

    assert len(intents) > 0
    total = sum(intent.notional for intent in intents)
    assert total <= Decimal("600")
    assert Decimal("900") - total >= Decimal("300")


def test_sweep_service_idempotent_same_payload_across_restarts(tmp_path):
    db_path = str(tmp_path / "state.db")
    first_store = StateStore(db_path=db_path)
    first_service = SweepService(
        state_store=first_store,
        target_try=300.0,
        offset_bps=0,
        default_min_notional=10.0,
    )

    balances = [Balance(asset="TRY", free=1000.0)]
    bids = {"BTC_TRY": 100000.0, "ETH_TRY": 50000.0, "SOL_TRY": 1000.0}

    first = first_service.build_order_intents(
        cycle_id="202601010102",
        balances=balances,
        symbols=["BTC_TRY", "ETH_TRY", "SOL_TRY"],
        best_bids=bids,
    )

    second_store = StateStore(db_path=db_path)
    second_service = SweepService(
        state_store=second_store,
        target_try=300.0,
        offset_bps=0,
        default_min_notional=10.0,
    )
    second = second_service.build_order_intents(
        cycle_id="202601010103",
        balances=balances,
        symbols=["BTC_TRY", "ETH_TRY", "SOL_TRY"],
        best_bids=bids,
    )

    assert len(first) > 0
    assert second == []


def test_sweep_service_small_excess_still_makes_progress(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(
        state_store=store, target_try=300.0, offset_bps=0, default_min_notional=10
    )

    balances = [Balance(asset="TRY", free=330.0)]
    bids = {"BTC_TRY": 100000.0, "ETH_TRY": 50000.0, "SOL_TRY": 1000.0}

    intents = service.build_order_intents(
        cycle_id="202601010104",
        balances=balances,
        symbols=["BTC_TRY", "ETH_TRY", "SOL_TRY"],
        best_bids=bids,
    )

    assert len(intents) >= 1
    assert sum(intent.notional for intent in intents) <= 30.0


def test_sweep_rounding_never_exceeds_budget(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(
        state_store=store, target_try=0.0, offset_bps=0, default_min_notional=0.01
    )

    balances = [Balance(asset="TRY", free=1.0)]
    bids = {"BTC_TRY": 0.333333333}

    intents = service.build_order_intents(
        cycle_id="202601010105",
        balances=balances,
        symbols=["BTC_TRY"],
        best_bids=bids,
    )

    assert len(intents) == 1
    assert intents[0].notional <= 1.0


def test_sweep_skips_missing_and_zero_bid(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(state_store=store, target_try=0.0, offset_bps=0, default_min_notional=10)

    balances = [Balance(asset="TRY", free=100.0)]
    bids = {"BTC_TRY": 0.0}

    intents = service.build_order_intents(
        cycle_id="202601010106",
        balances=balances,
        symbols=["BTC_TRY", "ETH_TRY"],
        best_bids=bids,
    )

    assert intents == []


def test_sweep_service_is_deterministic_and_sorted(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(
        state_store=store, target_try=100.0, offset_bps=0, default_min_notional=10.0
    )

    balances = [Balance(asset="TRY", free=400.0)]
    bids = {"SOL_TRY": 1000.0, "BTC_TRY": 100000.0, "ETH_TRY": 50000.0}

    intents = service.build_order_intents(
        cycle_id="202601010107",
        balances=balances,
        symbols=["SOL_TRY", "BTC_TRY", "ETH_TRY"],
        best_bids=bids,
    )

    assert [intent.symbol for intent in intents] == sorted(intent.symbol for intent in intents)


def test_sweep_min_notional_prevents_tiny_orders(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(
        state_store=store, target_try=0.0, offset_bps=0, default_min_notional=50.0
    )

    balances = [Balance(asset="TRY", free=10.0)]
    bids = {"BTC_TRY": 100000.0}

    intents = service.build_order_intents(
        cycle_id="202601010108",
        balances=balances,
        symbols=["BTC_TRY"],
        best_bids=bids,
    )

    assert intents == []


def test_sweep_rounding_matches_tick_and_step_sizes(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(
        state_store=store, target_try=0.0, offset_bps=0, default_min_notional=1.0
    )

    balances = [Balance(asset="TRY", free=100.0)]
    bids = {"BTC_TRY": 123.456}
    rules = {
        "BTC_TRY": SymbolInfo(
            symbol="BTC_TRY",
            base_asset="BTC",
            tick_size=0.05,
            step_size=0.003,
            min_notional=1.0,
        )
    }

    intents = service.build_order_intents(
        cycle_id="202601010109",
        balances=balances,
        symbols=["BTC_TRY"],
        best_bids=bids,
        symbol_rules=rules,
    )

    assert len(intents) == 1
    assert abs(
        (intents[0].price / Decimal("0.05")) - round(intents[0].price / Decimal("0.05"))
    ) < Decimal("1e-9")
    assert abs(
        (intents[0].quantity / Decimal("0.003")) - round(intents[0].quantity / Decimal("0.003"))
    ) < Decimal("1e-9")


def test_sweep_offset_reduces_price_before_rounding(tmp_path):
    store = StateStore(db_path=str(tmp_path / "state.db"))
    service = SweepService(
        state_store=store, target_try=0.0, offset_bps=20, default_min_notional=1.0
    )

    balances = [Balance(asset="TRY", free=100.0)]
    bids = {"BTC_TRY": 100.0}
    rules = {
        "BTC_TRY": SymbolInfo(
            symbol="BTC_TRY",
            base_asset="BTC",
            tick_size=0.1,
            step_size=0.01,
            min_notional=1.0,
        )
    }

    intents = service.build_order_intents(
        cycle_id="202601010110",
        balances=balances,
        symbols=["BTC_TRY"],
        best_bids=bids,
        symbol_rules=rules,
    )

    assert len(intents) == 1
    assert intents[0].price == Decimal("99.8")
