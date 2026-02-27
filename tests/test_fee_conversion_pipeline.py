from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.domain.risk_budget import Mode, RiskLimits
from btcbot.services.ledger_service import LedgerService, PnlReport
from btcbot.services.price_conversion_service import MarkPriceConverter
from btcbot.services.risk_budget_service import RiskBudgetService
from btcbot.services.state_store import StateStore


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_daily_drawdown_try=Decimal("1000"),
        max_drawdown_try=Decimal("5000"),
        max_gross_exposure_try=Decimal("100000"),
        max_position_pct=Decimal("1"),
        max_order_notional_try=Decimal("100000"),
        max_fee_try_per_day=Decimal("5000"),
    )


def test_mark_price_converter_try_and_non_try() -> None:
    converter = MarkPriceConverter({"USDTTRY": Decimal("35")})
    assert converter("TRY", "TRY") == Decimal("1")
    assert converter("USDT", "TRY") == Decimal("35")


def test_risk_budget_fees_try_today_uses_converted_total(tmp_path) -> None:
    store = StateStore(str(tmp_path / "risk_fees.sqlite"))
    service = RiskBudgetService(store, now_provider=lambda: datetime(2026, 1, 2, tzinfo=UTC))
    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("1"), "USDT": Decimal("2")},
        per_symbol=[],
        equity_estimate=Decimal("1000"),
        fees_total_try=Decimal("71"),
        fee_conversion_missing_currencies=(),
    )

    decision, *_ = service.compute_decision(
        limits=_risk_limits(),
        pnl_report=pnl_report,
        positions=[],
        mark_prices={},
        realized_today_try=Decimal("0"),
        kill_switch_active=False,
    )
    assert decision.risk_decision.signals.fees_try_today == Decimal("71")


def test_risk_budget_fail_closed_on_missing_fee_conversion_rate(tmp_path) -> None:
    store = StateStore(str(tmp_path / "risk_fail_closed.sqlite"))
    service = RiskBudgetService(store, now_provider=lambda: datetime(2026, 1, 2, tzinfo=UTC))
    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"USDT": Decimal("2")},
        per_symbol=[],
        equity_estimate=Decimal("1000"),
        fees_total_try=Decimal("0"),
        fee_conversion_missing_currencies=("USDT",),
    )

    decision, *_ = service.compute_decision(
        limits=_risk_limits(),
        pnl_report=pnl_report,
        positions=[],
        mark_prices={},
        realized_today_try=Decimal("0"),
        kill_switch_active=False,
    )
    assert decision.mode == Mode.OBSERVE_ONLY
    assert decision.risk_decision.reasons == ["fee_conversion_missing_rate"]


def test_ledger_report_marks_missing_fee_conversion_for_non_try_currency(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "fees.db"))
    store.append_ledger_events(
        [
            LedgerEvent(
                event_id="fee-1",
                ts=datetime(2026, 1, 2, tzinfo=UTC),
                symbol="BTCTRY",
                type=LedgerEventType.FEE,
                side=None,
                qty=Decimal("0"),
                price=None,
                fee=Decimal("2"),
                fee_currency="USDT",
                exchange_trade_id="fee-1",
                exchange_order_id=None,
                client_order_id=None,
                meta={},
            )
        ]
    )
    ledger = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))

    report = ledger.report(mark_prices={}, cash_try=Decimal("0"))
    assert report.fees_total_try == Decimal("0")
    assert report.fee_conversion_missing_currencies == ("USDT",)

    report_with_rate = ledger.report(
        mark_prices={"USDTTRY": Decimal("35")},
        cash_try=Decimal("0"),
        price_for_fee_conversion=MarkPriceConverter({"USDTTRY": Decimal("35")}),
    )
    assert report_with_rate.fees_total_try == Decimal("70")
    assert report_with_rate.fee_conversion_missing_currencies == ()


def test_financial_breakdown_fails_closed_without_non_try_fee_conversion_rate(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "fees_strict.db"))
    store.append_ledger_events(
        [
            LedgerEvent(
                event_id="fee-1",
                ts=datetime(2026, 1, 2, tzinfo=UTC),
                symbol="BTCTRY",
                type=LedgerEventType.FEE,
                side=None,
                qty=Decimal("0"),
                price=None,
                fee=Decimal("2"),
                fee_currency="USDT",
                exchange_trade_id="fee-1",
                exchange_order_id=None,
                client_order_id=None,
                meta={},
            )
        ]
    )
    ledger = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))

    import pytest

    from btcbot.ports_price_conversion import FeeConversionRateError

    with pytest.raises(FeeConversionRateError, match="USDT/TRY"):
        ledger.financial_breakdown(
            mark_prices={},
            cash_try=Decimal("0"),
            strict_fee_conversion=True,
        )
