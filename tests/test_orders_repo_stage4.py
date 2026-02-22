from __future__ import annotations

from decimal import Decimal

import pytest

from btcbot.persistence.uow import UnitOfWorkFactory


def test_stage4_write_blocked_in_read_only(tmp_path) -> None:
    factory = UnitOfWorkFactory(str(tmp_path / "state.sqlite"), read_only=True)
    with pytest.raises(PermissionError):
        with factory() as uow:
            uow.orders.record_stage4_order_submitted(
                symbol="BTCTRY",
                client_order_id="cid-1",
                exchange_client_id="x-cid-1",
                exchange_order_id="ex-1",
                side="buy",
                price=Decimal("100"),
                qty=Decimal("0.1"),
                mode="live",
            )


def test_stage4_submit_query_update_and_open_list(tmp_path) -> None:
    factory = UnitOfWorkFactory(str(tmp_path / "state.sqlite"))
    with factory() as uow:
        uow.orders.record_stage4_order_submitted(
            symbol="BTCTRY",
            client_order_id="cid-2",
            exchange_client_id="x-cid-2",
            exchange_order_id="ex-2",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("0.2"),
            mode="live",
            status="open",
        )

    with factory() as uow:
        order = uow.orders.get_stage4_order_by_client_id("cid-2")
        assert order is not None
        assert order.exchange_order_id == "ex-2"
        assert order.status == "open"

        uow.orders.record_stage4_order_cancel_requested("cid-2")

    with factory() as uow:
        open_orders = uow.orders.list_stage4_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0].client_order_id == "cid-2"
        assert open_orders[0].status == "cancel_requested"


def test_stage4_dedupe_status_deterministic(tmp_path) -> None:
    factory = UnitOfWorkFactory(str(tmp_path / "state.sqlite"))
    with factory() as uow:
        cold = uow.orders.stage4_submit_dedupe_status(
            internal_client_order_id="missing",
            exchange_client_order_id="missing-ex",
        )
        assert cold.should_dedupe is False
        assert cold.reason is None

        uow.orders.record_stage4_order_submitted(
            symbol="BTCTRY",
            client_order_id="cid-3",
            exchange_client_id="x-cid-3",
            exchange_order_id="ex-3",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("0.05"),
            mode="live",
            status="open",
        )

    with factory() as uow:
        hot = uow.orders.stage4_submit_dedupe_status(
            internal_client_order_id="cid-3",
            exchange_client_order_id="x-cid-3",
        )
        assert hot.should_dedupe is True
        assert hot.reason == "open_order_exists"
        assert hot.related_status == "open"
