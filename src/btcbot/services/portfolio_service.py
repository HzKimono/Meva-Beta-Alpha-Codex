from __future__ import annotations

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.models import Balance


class PortfolioService:
    def __init__(self, exchange: ExchangeClient):
        self.exchange = exchange

    def get_balances(self) -> list[Balance]:
        return self.exchange.get_balances()
