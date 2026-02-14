from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import Balance, OpenOrders, Order, OrderSide, PairInfo
from btcbot.services.market_data_replay import MarketDataReplay


class ReplayExchangeClient(ExchangeClient):
    def __init__(
        self,
        *,
        replay: MarketDataReplay,
        symbols: list[str],
        quote_asset: str = "TRY",
        balance_try: Decimal = Decimal("1000000"),
    ) -> None:
        self._replay = replay
        self._symbols = sorted(set(symbols))
        self._quote_asset = quote_asset
        self._balance_try = Decimal(str(balance_try))

    def get_balances(self) -> list[Balance]:
        return [Balance(asset=self._quote_asset, free=float(self._balance_try), locked=0.0)]

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del limit
        bid, ask = self._replay.get_orderbook(symbol)
        return float(bid), float(ask)

    def get_exchange_info(self) -> list[PairInfo]:
        out: list[PairInfo] = []
        for symbol in self._symbols:
            out.append(
                PairInfo.model_validate(
                    {
                        "pairSymbol": symbol,
                        "numeratorScale": 8,
                        "denominatorScale": 2,
                        "minTotalAmount": "10",
                        "minQuantity": "0.00000001",
                        "tickSize": "0.01",
                        "stepSize": "0.00000001",
                    }
                )
            )
        return out

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        del pair_symbol
        return OpenOrders.model_validate({"bids": [], "asks": []})

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ) -> Order:
        return Order(
            order_id=client_order_id or f"replay:{symbol}:{side}:{price}:{quantity}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
        )

    def cancel_order(self, order_id: str) -> bool:
        del order_id
        return True

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        del symbol
        return []

    def get_ticker_stats(self) -> list[dict[str, object]]:
        return self._replay.get_ticker_stats()

    def get_candles(self, symbol: str, limit: int) -> list[dict[str, object]]:
        rows = self._replay.get_candles(symbol, limit)
        return [
            {
                "ts": int(item.ts.timestamp()),
                "open": str(item.open),
                "high": str(item.high),
                "low": str(item.low),
                "close": str(item.close),
                "volume": str(item.volume),
            }
            for item in rows
        ]

    def now(self) -> datetime:
        return self._replay.now()
