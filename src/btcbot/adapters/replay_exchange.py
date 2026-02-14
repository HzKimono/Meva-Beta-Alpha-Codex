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
        balances: dict[str, Decimal] | None = None,
        pair_info_snapshot: list[PairInfo | dict[str, object]] | None = None,
    ) -> None:
        self._replay = replay
        self._symbols = sorted(set(symbols))
        self._quote_asset = quote_asset
        base_balances = balances or {quote_asset: Decimal(str(balance_try))}
        self._balances = {
            str(asset).upper(): Decimal(str(amount)) for asset, amount in base_balances.items()
        }
        self._pair_info_snapshot = pair_info_snapshot

    def get_balances(self) -> list[Balance]:
        balances: list[Balance] = []
        for asset, amount in sorted(self._balances.items()):
            quant = Decimal("0.01") if asset == "TRY" else Decimal("0.00000001")
            balances.append(Balance(asset=asset, free=float(amount.quantize(quant)), locked=0.0))
        return balances

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del limit
        top = self._replay.get_orderbook(symbol)
        if top is None:
            raise KeyError(f"orderbook not found for {symbol}")
        bid, ask = top
        return float(bid), float(ask)

    def get_exchange_info(self) -> list[PairInfo]:
        if self._pair_info_snapshot is not None:
            return [
                item if isinstance(item, PairInfo) else PairInfo.model_validate(item)
                for item in self._pair_info_snapshot
            ]

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
