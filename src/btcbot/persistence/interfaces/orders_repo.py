from __future__ import annotations

from typing import Protocol


class OrdersRepoProtocol(Protocol):
    def client_order_id_exists(self, client_order_id: str) -> bool: ...
