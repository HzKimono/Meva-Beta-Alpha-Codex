from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.services.ledger_service import LedgerService
from btcbot.services.state_store import StateStore


def _fee_event(event_id: str, fee: str) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        symbol="BTCTRY",
        type=LedgerEventType.FEE,
        side=None,
        qty=Decimal("0"),
        price=None,
        fee=Decimal(fee),
        fee_currency="TRY",
        exchange_trade_id=event_id,
        exchange_order_id=None,
        client_order_id=None,
        meta={},
    )


def test_ledger_service_reducer_applies_delta_events_only(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger_incremental.db"))
    service = LedgerService(state_store=store, logger=__import__("logging").getLogger(__name__))

    store.append_ledger_events([_fee_event("fee-1", "1"), _fee_event("fee-2", "2")])
    service.report(mark_prices={}, cash_try=Decimal("0"))
    assert service.last_reduce_delta_events == 2

    service.report(mark_prices={}, cash_try=Decimal("0"))
    assert service.last_reduce_delta_events == 0

    store.append_ledger_events([_fee_event("fee-3", "3")])
    service.report(mark_prices={}, cash_try=Decimal("0"))
    assert service.last_reduce_delta_events == 1
