from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.domain.risk_budget import RiskLimits
from btcbot.domain.stage4 import Fill
from btcbot.risk.budget import RiskBudgetPolicy, SelfFinancingPolicy
from btcbot.services.decision_pipeline_service import DecisionPipelineService
from btcbot.services.ledger_service import LedgerService, PnlReport
from btcbot.services.risk_budget_service import CapitalPolicyError, RiskBudgetService
from btcbot.services.state_store import StateStore


def _decision_payloads(caplog):
    payloads = []
    for rec in caplog.records:
        if rec.msg == "decision_event":
            payloads.append(rec.__dict__.get("extra", {}))
    return payloads


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_daily_drawdown_try=Decimal("100"),
        max_drawdown_try=Decimal("200"),
        max_gross_exposure_try=Decimal("500"),
        max_position_pct=Decimal("0.5"),
        max_order_notional_try=Decimal("100"),
        max_fee_try_per_day=Decimal("20"),
    )


def test_apply_self_financing_split_positive_negative_zero() -> None:
    policy = RiskBudgetPolicy(
        SelfFinancingPolicy(
            profit_compound_ratio=Decimal("0.60"),
            profit_treasury_ratio=Decimal("0.40"),
        )
    )

    pos_cap, pos_treasury = policy.apply_self_financing(
        trading_capital_try=Decimal("100"),
        treasury_try=Decimal("20"),
        realized_pnl_delta_try=Decimal("10"),
    )
    assert pos_cap == Decimal("106.00000000")
    assert pos_treasury == Decimal("24.00000000")

    neg_cap, neg_treasury = policy.apply_self_financing(
        trading_capital_try=Decimal("100"),
        treasury_try=Decimal("20"),
        realized_pnl_delta_try=Decimal("-8"),
    )
    assert neg_cap == Decimal("92.00000000")
    assert neg_treasury == Decimal("20.00000000")

    zero_cap, zero_treasury = policy.apply_self_financing(
        trading_capital_try=Decimal("100"),
        treasury_try=Decimal("20"),
        realized_pnl_delta_try=Decimal("0"),
    )
    assert zero_cap == Decimal("100.00000000")
    assert zero_treasury == Decimal("20.00000000")


def test_apply_self_financing_rounding_decimal_boundaries() -> None:
    policy = RiskBudgetPolicy(
        SelfFinancingPolicy(
            profit_compound_ratio=Decimal("0.33333333"),
            profit_treasury_ratio=Decimal("0.66666667"),
        )
    )
    cap, treasury = policy.apply_self_financing(
        trading_capital_try=Decimal("1"),
        treasury_try=Decimal("2"),
        realized_pnl_delta_try=Decimal("0.010000005"),
    )
    assert cap == Decimal("1.00333334")
    assert treasury == Decimal("2.00666667")


def test_self_financing_checkpoint_idempotent_same_event_count_noop(caplog, tmp_path) -> None:
    caplog.set_level(logging.INFO)
    store = StateStore(str(tmp_path / "cap.db"))
    service = RiskBudgetService(store)

    first = service.apply_self_financing_checkpoint(
        cycle_id="c1",
        realized_pnl_total_try=Decimal("10"),
        ledger_event_count=3,
        ledger_checkpoint_id="first-checkpoint",
        seed_trading_capital_try=Decimal("100"),
    )
    second = service.apply_self_financing_checkpoint(
        cycle_id="c1-retry",
        realized_pnl_total_try=Decimal("10"),
        ledger_event_count=3,
        ledger_checkpoint_id="different-audit-id",
        seed_trading_capital_try=Decimal("100"),
    )

    assert first.applied is True
    assert second.applied is False
    assert first.trading_capital_try == second.trading_capital_try
    assert first.treasury_try == second.treasury_try

    payloads = _decision_payloads(caplog)
    assert any(
        p.get("cycle_id") and p.get("decision_layer") and p.get("reason_code") and p.get("action")
        for p in payloads
    )
    capital_events = [p for p in payloads if p.get("decision_layer") == "capital_policy"]
    assert capital_events and all(p.get("scope") == "global" for p in capital_events)


def test_self_financing_non_monotonic_event_count_fails_closed(caplog, tmp_path) -> None:
    caplog.set_level(logging.INFO)
    store = StateStore(str(tmp_path / "monotonic.db"))
    service = RiskBudgetService(store)

    first = service.apply_self_financing_checkpoint(
        cycle_id="c10",
        realized_pnl_total_try=Decimal("20"),
        ledger_event_count=10,
        ledger_checkpoint_id="cp-10",
        seed_trading_capital_try=Decimal("100"),
    )
    assert first.applied is True

    with pytest.raises(CapitalPolicyError):
        service.apply_self_financing_checkpoint(
            cycle_id="c9",
            realized_pnl_total_try=Decimal("20"),
            ledger_event_count=9,
            ledger_checkpoint_id="cp-9",
            seed_trading_capital_try=Decimal("100"),
        )

    event = _decision_payloads(caplog)[-1]
    assert event["reason_code"] == "capital_error:non_monotonic_checkpoint"
    assert event["action"] == "BLOCK"
    assert event["scope"] == "global"


def test_self_financing_negative_capital_fails_closed(caplog, tmp_path) -> None:
    caplog.set_level(logging.INFO)
    store = StateStore(str(tmp_path / "negative.db"))
    service = RiskBudgetService(store)

    with pytest.raises(CapitalPolicyError):
        service.apply_self_financing_checkpoint(
            cycle_id="c-neg",
            realized_pnl_total_try=Decimal("-200"),
            ledger_event_count=5,
            ledger_checkpoint_id="cp-neg",
            seed_trading_capital_try=Decimal("100"),
        )

    event = _decision_payloads(caplog)[-1]
    assert event["reason_code"] == "capital_error:negative_capital"
    assert event["action"] == "BLOCK"
    assert event["scope"] == "global"


def test_compute_decision_uses_persisted_capital_state(tmp_path) -> None:
    store = StateStore(str(tmp_path / "compute.db"))
    store.upsert_capital_policy_state(
        trading_capital_try=Decimal("1234"),
        treasury_try=Decimal("56"),
        last_realized_pnl_total_try=Decimal("10"),
        last_event_count=7,
        last_checkpoint_id="cp-7",
        last_cycle_id="seed",
    )
    service = RiskBudgetService(store)
    report = PnlReport(
        realized_pnl_total=Decimal("10"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("0")},
        per_symbol=[],
        equity_estimate=Decimal("1290"),
    )

    decision, *_ = service.compute_decision(
        limits=_risk_limits(),
        pnl_report=report,
        positions=[],
        mark_prices={},
        realized_today_try=Decimal("0"),
        kill_switch_active=False,
    )

    assert decision.budget_view.trading_capital_try == Decimal("1234.00000000")
    assert decision.budget_view.treasury_try == Decimal("56.00000000")
    assert decision.budget_view.available_risk_capital_try == Decimal("1234.00000000")
    assert decision.budget_view.max_order_notional_try == Decimal("246.80000000")


def test_accounting_cycle_replay_same_fills_no_double_apply(tmp_path) -> None:
    store = StateStore(str(tmp_path / "replay.db"))
    ledger = LedgerService(store, logging.getLogger("test-ledger"))
    risk = RiskBudgetService(store)

    fill = Fill(
        fill_id="t1",
        order_id="o1",
        symbol="BTCTRY",
        side="BUY",
        price=Decimal("100"),
        qty=Decimal("1"),
        fee=Decimal("0"),
        fee_asset="TRY",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
    )
    sell = Fill(
        fill_id="t2",
        order_id="o2",
        symbol="BTCTRY",
        side="SELL",
        price=Decimal("110"),
        qty=Decimal("1"),
        fee=Decimal("0"),
        fee_asset="TRY",
        ts=datetime(2026, 1, 2, tzinfo=UTC),
    )

    ledger.ingest_exchange_updates([fill, sell])
    first_report = ledger.report(mark_prices={"BTCTRY": Decimal("110")}, cash_try=Decimal("1000"))
    checkpoint = ledger.checkpoint()
    first = risk.apply_self_financing_checkpoint(
        cycle_id="cycle-1",
        realized_pnl_total_try=first_report.realized_pnl_total,
        ledger_event_count=checkpoint.event_count,
        ledger_checkpoint_id=checkpoint.checkpoint_id,
        seed_trading_capital_try=Decimal("1000"),
    )

    ledger.ingest_exchange_updates([fill, sell])
    second_report = ledger.report(mark_prices={"BTCTRY": Decimal("110")}, cash_try=Decimal("1000"))
    second_checkpoint = ledger.checkpoint()
    second = risk.apply_self_financing_checkpoint(
        cycle_id="cycle-2-replay",
        realized_pnl_total_try=second_report.realized_pnl_total,
        ledger_event_count=second_checkpoint.event_count,
        ledger_checkpoint_id=second_checkpoint.checkpoint_id,
        seed_trading_capital_try=Decimal("1000"),
    )

    assert first_report.realized_pnl_total == second_report.realized_pnl_total
    assert checkpoint.checkpoint_id == second_checkpoint.checkpoint_id
    assert checkpoint.event_count == second_checkpoint.event_count
    assert first.applied is True
    assert second.applied is False


def test_cash_reserve_gate_emits_decision_event(caplog) -> None:
    caplog.set_level(logging.INFO)
    service = DecisionPipelineService.__new__(DecisionPipelineService)
    service._emit_capital_gate_events(
        cycle_id="cycle-gate",
        cash_try=Decimal("100"),
        try_cash_target=Decimal("150"),
        investable_total_try=Decimal("0"),
        investable_this_cycle_try=Decimal("0"),
    )

    payloads = _decision_payloads(caplog)
    assert payloads, "expected capital gate decision event"
    event = payloads[-1]
    for key in ("cycle_id", "decision_layer", "reason_code", "action"):
        assert key in event and event[key] is not None
    assert event["reason_code"] == "capital_gate:cash_reserve_target"
