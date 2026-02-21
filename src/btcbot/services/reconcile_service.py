from __future__ import annotations

from dataclasses import dataclass

from btcbot.domain.stage4 import Order


@dataclass(frozen=True)
class ReconcileResult:
    mark_unknown_closed: list[str]
    import_external: list[Order]
    enrich_exchange_ids: list[tuple[str, str]]
    external_missing_client_id: list[Order]


class ReconcileService:
    def resolve(
        self,
        *,
        exchange_open_orders: list[Order],
        db_open_orders: list[Order],
        failed_symbols: set[str] | None = None,
    ) -> ReconcileResult:
        blocked_symbols = {symbol.upper() for symbol in (failed_symbols or set())}
        exchange_by_client: dict[str, Order] = {}
        external_missing_client_id: list[Order] = []
        for order in exchange_open_orders:
            if order.client_order_id:
                exchange_by_client[order.client_order_id] = order
            else:
                external_missing_client_id.append(order)

        db_by_client = {
            order.client_order_id: order
            for order in db_open_orders
            if order.client_order_id and order.mode != "external"
        }

        mark_unknown_closed: list[str] = []
        enrich_exchange_ids: list[tuple[str, str]] = []
        for client_order_id, order in db_by_client.items():
            if str(order.symbol).upper() in blocked_symbols:
                continue
            exchange_match = exchange_by_client.get(client_order_id)
            if exchange_match is None:
                mark_unknown_closed.append(client_order_id)
                continue
            if (not order.exchange_order_id) and exchange_match.exchange_order_id:
                enrich_exchange_ids.append((client_order_id, exchange_match.exchange_order_id))

        import_external: list[Order] = []
        for client_order_id, order in exchange_by_client.items():
            if client_order_id not in db_by_client:
                import_external.append(Order(**{**order.__dict__, "mode": "external"}))

        return ReconcileResult(
            mark_unknown_closed=mark_unknown_closed,
            import_external=import_external,
            enrich_exchange_ids=enrich_exchange_ids,
            external_missing_client_id=external_missing_client_id,
        )
