from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.ledger import EquityPoint, compute_max_drawdown
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType
from btcbot.services.ledger_service import LedgerService
from btcbot.services.price_conversion_service import MarkPriceConverter
from btcbot.services.state_store import StateStore


def test_stage7_single_buy_then_sell_realized_net_of_fees(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "s7.db"))
    service = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    ts_sell = datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)
    buy = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="BUY",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="b1",
    )
    sell = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="SELL",
        price=Decimal("110"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="s1",
    )
    fills_buy = service.simulate_dry_run_fills(
        cycle_id="c1",
        actions=[buy],
        mark_prices={"BTCTRY": Decimal("100")},
        slippage_bps=Decimal("0"),
        fees_bps=Decimal("10"),
        ts=ts,
    )
    service.append_simulated_fills(fills_buy)
    fills_sell = service.simulate_dry_run_fills(
        cycle_id="c2",
        actions=[sell],
        mark_prices={"BTCTRY": Decimal("110")},
        slippage_bps=Decimal("0"),
        fees_bps=Decimal("10"),
        ts=ts_sell,
    )
    service.append_simulated_fills(fills_sell)

    snap = service.snapshot(
        mark_prices={"BTCTRY": Decimal("110")}, cash_try=Decimal("0"), ts=ts_sell
    )
    expected_gross = Decimal("10")
    expected_fees = Decimal("0.1") + Decimal("0.11")
    expected_net = expected_gross - expected_fees
    assert snap.realized_pnl_try == expected_gross
    assert snap.gross_pnl_try == expected_gross
    assert snap.net_pnl_try == expected_net


def test_stage7_fee_currency_conversion_hook(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "s7_fee.db"))
    from btcbot.domain.ledger import LedgerEvent, LedgerEventType

    store.append_ledger_events(
        [
            LedgerEvent(
                event_id="fee-1",
                ts=datetime(2024, 1, 1, tzinfo=UTC),
                symbol="BTCTRY",
                type=LedgerEventType.FEE,
                side=None,
                qty=Decimal("0"),
                price=None,
                fee=Decimal("2"),
                fee_currency="USDT",
                exchange_trade_id="fee:t1",
                exchange_order_id=None,
                client_order_id=None,
                meta={},
            )
        ]
    )
    service = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))
    snap = service.snapshot(
        mark_prices={},
        cash_try=Decimal("0"),
        price_for_fee_conversion=lambda base, quote: Decimal("35")
        if (base, quote) == ("USDT", "TRY")
        else Decimal("1"),
    )
    assert snap.fees_try == Decimal("70")
    assert snap.net_pnl_try == Decimal("-70")


def test_stage7_drawdown_computation() -> None:
    points = [
        EquityPoint(ts=datetime(2024, 1, 1, tzinfo=UTC), equity_try=Decimal("1000")),
        EquityPoint(ts=datetime(2024, 1, 2, tzinfo=UTC), equity_try=Decimal("1100")),
        EquityPoint(ts=datetime(2024, 1, 3, tzinfo=UTC), equity_try=Decimal("990")),
    ]
    assert compute_max_drawdown(points) == Decimal("0.1")


def test_stage7_simulated_fill_ids_are_deterministic_and_idempotent(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "s7_ids.db"))
    service = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="BUY",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="c-order",
        exchange_order_id="e-order",
    )

    fills_a = service.simulate_dry_run_fills(
        cycle_id="cycle-1",
        actions=[action],
        mark_prices={"BTCTRY": Decimal("100")},
        slippage_bps=Decimal("0"),
        fees_bps=Decimal("0"),
        ts=ts,
    )
    fills_b = service.simulate_dry_run_fills(
        cycle_id="cycle-1",
        actions=[action],
        mark_prices={"BTCTRY": Decimal("100")},
        slippage_bps=Decimal("0"),
        fees_bps=Decimal("0"),
        ts=ts,
    )

    assert fills_a[0].event.event_id == fills_b[0].event.event_id
    assert fills_a[0].event.exchange_trade_id == fills_b[0].event.exchange_trade_id

    first = service.append_simulated_fills(fills_a)
    second = service.append_simulated_fills(fills_b)
    assert first.events_inserted == 2
    assert second.events_ignored == 2


def test_stage7_simulated_non_try_fee_currency_is_convertible(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "s7_non_try_fee.db"))
    service = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    action = LifecycleAction(
        action_type=LifecycleActionType.SUBMIT,
        symbol="BTC_TRY",
        side="BUY",
        price=Decimal("100"),
        qty=Decimal("1"),
        reason="test",
        client_order_id="b1",
    )

    fills = service.simulate_dry_run_fills(
        cycle_id="c1",
        actions=[action],
        mark_prices={"BTCTRY": Decimal("100")},
        slippage_bps=Decimal("0"),
        fees_bps=Decimal("10"),
        ts=ts,
        fee_currency="USDT",
        fee_currency_to_try_rate=Decimal("35"),
    )
    service.append_simulated_fills(fills)

    snap = service.snapshot(
        mark_prices={"USDTTRY": Decimal("35")},
        cash_try=Decimal("0"),
        price_for_fee_conversion=MarkPriceConverter({"USDTTRY": Decimal("35")}),
    )
    assert snap.fees_try == Decimal("0.1")
