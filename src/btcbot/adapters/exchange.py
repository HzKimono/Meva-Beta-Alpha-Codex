from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import Balance, OpenOrders, Order, OrderSide, OrderSnapshot, PairInfo


class ExchangeClient(ABC):
    @abstractmethod
    def get_balances(self) -> list[Balance]:
        raise NotImplementedError

    @abstractmethod
    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[Decimal, Decimal]:
        """Return (best_bid, best_ask)."""
        raise NotImplementedError

    @abstractmethod
    def get_exchange_info(self) -> list[PairInfo]:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        raise NotImplementedError

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int) -> list[OrderSnapshot]:
        del pair_symbol, start_ms, end_ms
        raise NotImplementedError

    def get_order(self, order_id: str) -> OrderSnapshot:
        del order_id
        raise NotImplementedError

    @abstractmethod
    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> Order:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        raise NotImplementedError

    def get_recent_fills(self, pair_symbol: str, since_ms: int | None = None) -> list[TradeFill]:
        del pair_symbol, since_ms
        return []

    def get_ticker_stats(self) -> list[dict[str, object]]:
        return []

    def get_candles(self, symbol: str, limit: int) -> list[dict[str, object]]:
        del symbol, limit
        return []

    def close(self) -> None:
        """Release resources associated with the exchange client."""
        return None
