from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import Order


@dataclass(frozen=True)
class OrderAck:
    exchange_order_id: str
    status: str
    raw: dict[str, object] | None = None


class ExchangeClientStage4(Protocol):
    def get_exchange_info(self) -> list[PairInfo]: ...

    def list_open_orders(self, symbol: str) -> list[Order]: ...

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_order_id: str,
    ) -> OrderAck: ...

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool: ...

    def cancel_order_by_client_order_id(self, client_order_id: str) -> bool: ...

    def get_recent_fills(self, symbol: str, since_ms: int | None = None) -> list[TradeFill]: ...

    def close(self) -> None: ...
