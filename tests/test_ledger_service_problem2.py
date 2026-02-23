from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.domain.stage4 import Fill
from btcbot.ports_price_conversion import FeeConversionRateError
from btcbot.services.ledger_service import LedgerService
from btcbot.services.state_store import StateStore


def _fill(
    *, fill_id: str, side: str, price: str, qty: str, fee: str, fee_asset: str, ts: datetime
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=f"o-{fill_id}",
        symbol="BTC_TRY",
        side=side,
        price=Decimal(price),
        qty=Decimal(qty),
        fee=Decimal(fee),
        fee_asset=fee_asset,
        ts=ts,
    )


def test_ingest_exchange_updates_idempotent_and_pnl_stable(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "problem2_idempotent.db"))
    service = LedgerService(state_store=store, logger=logging.getLogger(__name__))

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    fills = [
        _fill(fill_id="t-buy", side="BUY", price="100", qty="1", fee="1", fee_asset="TRY", ts=t0),
        _fill(
            fill_id="t-sell",
            side="SELL",
            price="120",
            qty="1",
            fee="1",
            fee_asset="TRY",
            ts=t0 + timedelta(seconds=1),
        ),
    ]

    first = service.ingest_exchange_updates(fills)
    snap_first = service.snapshot(
        mark_prices={"BTCTRY": Decimal("120")}, cash_try=Decimal("0"), ts=t0
    )

    second = service.ingest_exchange_updates(fills)
    snap_second = service.snapshot(
        mark_prices={"BTCTRY": Decimal("120")}, cash_try=Decimal("0"), ts=t0
    )

    assert first.events_inserted == 4
    assert second.events_inserted == 0
    assert second.events_ignored == 4
    assert snap_second.realized_pnl_try == snap_first.realized_pnl_try
    assert snap_second.unrealized_pnl_try == snap_first.unrealized_pnl_try
    assert snap_second.net_pnl_try == snap_first.net_pnl_try


def test_realized_unrealized_and_fee_impact(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "problem2_pnl.db"))
    service = LedgerService(state_store=store, logger=logging.getLogger(__name__))

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    fills = [
        _fill(fill_id="t-buy", side="BUY", price="100", qty="1", fee="0", fee_asset="TRY", ts=t0),
        _fill(
            fill_id="t-sell",
            side="SELL",
            price="120",
            qty="1",
            fee="5",
            fee_asset="TRY",
            ts=t0 + timedelta(seconds=1),
        ),
    ]

    ingest = service.ingest_exchange_updates(fills)
    snap = service.snapshot(mark_prices={"BTCTRY": Decimal("120")}, cash_try=Decimal("0"), ts=t0)

    assert ingest.events_inserted == 3
    assert snap.realized_pnl_try == Decimal("20")
    assert snap.unrealized_pnl_try == Decimal("0")
    assert snap.fees_try == Decimal("5")
    assert snap.net_pnl_try == Decimal("15")


def test_fee_conversion_missing_and_available_rates(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "problem2_fee_conversion.db"))
    service = LedgerService(state_store=store, logger=logging.getLogger(__name__))

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    fills = [
        _fill(fill_id="t-buy", side="BUY", price="100", qty="1", fee="0.1", fee_asset="USDT", ts=t0)
    ]
    service.ingest_exchange_updates(fills)

    report_missing = service.report(
        mark_prices={}, cash_try=Decimal("0"), price_for_fee_conversion=None
    )
    assert report_missing.fees_total_try == Decimal("0")
    assert report_missing.fee_conversion_missing_currencies == ("USDT",)

    report_with_converter = service.report(
        mark_prices={},
        cash_try=Decimal("0"),
        price_for_fee_conversion=lambda base, quote: Decimal("35")
        if (base, quote) == ("USDT", "TRY")
        else Decimal("0"),
    )
    assert report_with_converter.fees_total_try == Decimal("3.5")
    assert report_with_converter.fee_conversion_missing_currencies == ()

    def _missing_rate(base: str, quote: str) -> Decimal:
        del base, quote
        raise FeeConversionRateError("missing")

    strict_raised = False
    try:
        service._compute_fees_try(
            fees_by_currency={"USDT": Decimal("0.1")},
            price_for_fee_conversion=_missing_rate,
            strict=True,
        )
    except FeeConversionRateError:
        strict_raised = True
    assert strict_raised is True
