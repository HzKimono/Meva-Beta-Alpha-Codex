from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.stage4 import Fill
from btcbot.services.ledger_service import LedgerService
from btcbot.services.state_store import StateStore


def test_ingest_twice_is_idempotent(tmp_path) -> None:
    store = StateStore(db_path=str(tmp_path / "ledger_service.db"))
    service = LedgerService(state_store=store, logger=logging.getLogger("test"))

    fills = [
        Fill(
            fill_id="t-1",
            order_id="o-1",
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("1"),
            fee=Decimal("1"),
            fee_asset="TRY",
            ts=datetime(2024, 1, 1, tzinfo=UTC),
        ),
        Fill(
            fill_id="t-2",
            order_id="o-2",
            symbol="BTC_TRY",
            side="sell",
            price=Decimal("120"),
            qty=Decimal("0.5"),
            fee=Decimal("0.5"),
            fee_asset="TRY",
            ts=datetime(2024, 1, 2, tzinfo=UTC),
        ),
    ]

    first = service.ingest_exchange_updates(fills)
    first_report = service.report({"BTCTRY": Decimal("130")})

    second = service.ingest_exchange_updates(fills)
    second_report = service.report({"BTCTRY": Decimal("130")})

    assert first.events_inserted == 4
    assert second.events_inserted == 0
    rows = store.load_ledger_events()
    assert len(rows) == 4
    fee_events = [event for event in rows if event.type.value == "FEE"]
    assert sorted(event.exchange_trade_id for event in fee_events) == ["fee:t-1", "fee:t-2"]
    assert all(event.side is None for event in fee_events)
    assert all(event.qty == Decimal("0") for event in fee_events)
    assert all(event.price is None for event in fee_events)
    assert first_report.realized_pnl_total == second_report.realized_pnl_total
    assert first_report.unrealized_pnl_total == second_report.unrealized_pnl_total
