from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.accounting.accounting_service import AccountingService
from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import OrderSide
from btcbot.services.state_store import StateStore


class FakeExchange:
    def __init__(self, fills_by_symbol):
        self.fills_by_symbol = fills_by_symbol

    def get_recent_fills(self, pair_symbol: str):
        return self.fills_by_symbol.get(pair_symbol, [])


def test_buy_buy_avg_cost(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    fills = {
        "BTC_TRY": [
            TradeFill(
                "f1",
                "o1",
                "BTC_TRY",
                OrderSide.BUY,
                Decimal("100"),
                Decimal("1"),
                Decimal("1"),
                "TRY",
                datetime.now(UTC),
            ),
            TradeFill(
                "f2",
                "o2",
                "BTC_TRY",
                OrderSide.BUY,
                Decimal("120"),
                Decimal("1"),
                Decimal("1"),
                "TRY",
                datetime.now(UTC),
            ),
        ]
    }
    svc = AccountingService(exchange=FakeExchange(fills), state_store=store)
    svc.refresh(["BTC_TRY"], {"BTCTRY": Decimal("130")})

    pos = store.get_position("BTC_TRY")
    assert pos is not None
    assert pos.qty == Decimal("2")
    assert pos.avg_cost == Decimal("111")


def test_partial_and_full_sell(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    fills = {
        "BTC_TRY": [
            TradeFill(
                "f1",
                "o1",
                "BTC_TRY",
                OrderSide.BUY,
                Decimal("100"),
                Decimal("2"),
                Decimal("0"),
                "TRY",
                datetime.now(UTC),
            ),
            TradeFill(
                "f2",
                "o2",
                "BTC_TRY",
                OrderSide.SELL,
                Decimal("110"),
                Decimal("1"),
                Decimal("1"),
                "TRY",
                datetime.now(UTC),
            ),
            TradeFill(
                "f3",
                "o3",
                "BTC_TRY",
                OrderSide.SELL,
                Decimal("120"),
                Decimal("1"),
                Decimal("1"),
                "TRY",
                datetime.now(UTC),
            ),
        ]
    }
    svc = AccountingService(exchange=FakeExchange(fills), state_store=store)
    svc.refresh(["BTC_TRY"], {"BTCTRY": Decimal("120")})

    pos = store.get_position("BTC_TRY")
    assert pos is not None
    assert pos.qty == Decimal("0")
    assert pos.avg_cost == Decimal("0")
    assert pos.realized_pnl == Decimal("28")
    assert pos.fees_paid == Decimal("2")


def test_sell_fee_proration_when_sell_qty_is_clamped(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    fills = {
        "BTC_TRY": [
            TradeFill(
                "f1",
                "o1",
                "BTC_TRY",
                OrderSide.BUY,
                Decimal("100"),
                Decimal("1"),
                Decimal("0"),
                "TRY",
                datetime.now(UTC),
            ),
            TradeFill(
                "f2",
                "o2",
                "BTC_TRY",
                OrderSide.SELL,
                Decimal("120"),
                Decimal("2"),
                Decimal("10"),
                "TRY",
                datetime.now(UTC),
            ),
        ]
    }
    svc = AccountingService(exchange=FakeExchange(fills), state_store=store)

    svc.refresh(["BTC_TRY"], {"BTCTRY": Decimal("120")})

    pos = store.get_position("BTC_TRY")
    assert pos is not None
    # sell_qty is clamped to 1, fee_used should be 10 * (1 / 2) = 5
    assert pos.realized_pnl == Decimal("15")
    assert pos.fees_paid == Decimal("5")


def test_fee_currency_not_quote_is_ignored_with_warning(tmp_path, caplog) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    fills = {
        "BTC_TRY": [
            TradeFill(
                "f1",
                "o1",
                "BTC_TRY",
                OrderSide.BUY,
                Decimal("100"),
                Decimal("1"),
                Decimal("2"),
                "USDT",
                datetime.now(UTC),
            ),
        ]
    }
    caplog.set_level(logging.WARNING)
    svc = AccountingService(exchange=FakeExchange(fills), state_store=store)

    svc.refresh(["BTC_TRY"], {"BTCTRY": Decimal("120")})

    pos = store.get_position("BTC_TRY")
    assert pos is not None
    assert pos.avg_cost == Decimal("100")
    assert pos.fees_paid == Decimal("0")
    assert "Ignoring non-quote fee currency in accounting" in caplog.text


def test_fee_currency_matches_non_try_quote_currency(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "state.db"))
    fills = {
        "BTC_USDT": [
            TradeFill(
                "f-usdt",
                "o-usdt",
                "BTC_USDT",
                OrderSide.BUY,
                Decimal("100"),
                Decimal("1"),
                Decimal("2"),
                "USDT",
                datetime.now(UTC),
            ),
        ]
    }
    svc = AccountingService(exchange=FakeExchange(fills), state_store=store)

    svc.refresh(["BTC_USDT"], {"BTCUSDT": Decimal("120")})

    pos = store.get_position("BTC_USDT")
    assert pos is not None
    assert pos.avg_cost == Decimal("102")
    assert pos.fees_paid == Decimal("2")
