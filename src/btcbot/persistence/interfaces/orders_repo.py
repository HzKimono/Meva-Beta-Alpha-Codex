from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Stage4SubmitDedupeStatus:
    should_dedupe: bool
    dedupe_key: str
    reason: str | None = None
    age_seconds: int | None = None
    related_order_id: str | None = None
    related_status: str | None = None


class OrdersRepoProtocol(Protocol):
    def client_order_id_exists(self, client_order_id: str) -> bool: ...

    def stage4_has_unknown_orders(self) -> bool: ...

    def stage4_unknown_client_order_ids(self) -> list[str]: ...

    def get_stage4_order_by_client_id(self, client_order_id: str): ...

    def list_stage4_open_orders(
        self,
        symbol: str | None = None,
        *,
        include_external: bool = False,
        include_unknown: bool = False,
    ): ...

    def is_order_terminal(self, client_order_id: str) -> bool: ...

    def stage4_submit_dedupe_status(
        self,
        *,
        internal_client_order_id: str,
        exchange_client_order_id: str,
    ) -> Stage4SubmitDedupeStatus: ...

    def record_stage4_order_submitted(
        self,
        *,
        symbol: str,
        client_order_id: str,
        exchange_client_id: str | None = None,
        exchange_order_id: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        mode: str,
        status: str = "open",
    ) -> None: ...

    def record_stage4_order_simulated_submit(
        self,
        *,
        symbol: str,
        client_order_id: str,
        side: str,
        price: Decimal,
        qty: Decimal,
    ) -> None: ...

    def record_stage4_order_cancel_requested(self, client_order_id: str) -> None: ...

    def record_stage4_order_canceled(self, client_order_id: str) -> None: ...

    def record_stage4_order_error(
        self,
        *,
        client_order_id: str,
        reason: str,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        mode: str,
        status: str = "error",
    ) -> None: ...

    def record_stage4_order_rejected(
        self,
        client_order_id: str,
        reason: str,
        *,
        symbol: str = "UNKNOWN",
        side: str = "unknown",
        price: Decimal = Decimal("0"),
        qty: Decimal = Decimal("0"),
        mode: str = "dry_run",
    ) -> None: ...

    def update_stage4_order_exchange_id(self, client_order_id: str, exchange_order_id: str) -> None: ...

    def mark_stage4_unknown_closed(self, client_order_id: str) -> None: ...

    def import_stage4_external_order(self, order: object) -> None: ...

    def get_stage4_order_by_exchange_id(self, exchange_order_id: str): ...
