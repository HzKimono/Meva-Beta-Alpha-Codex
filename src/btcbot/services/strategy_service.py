from __future__ import annotations

from decimal import Decimal

from btcbot.accounting.accounting_service import AccountingService
from btcbot.config import Settings
from btcbot.domain.intent import Intent
from btcbot.domain.symbols import canonical_symbol
from btcbot.services.market_data_service import MarketDataService
from btcbot.services.state_store import PENDING_GRACE_SECONDS, StateStore
from btcbot.strategies.base import Strategy
from btcbot.strategies.context import StrategyContext


class StrategyService:
    def __init__(
        self,
        *,
        strategy: Strategy,
        settings: Settings,
        market_data_service: MarketDataService,
        accounting_service: AccountingService,
        state_store: StateStore,
    ) -> None:
        self.strategy = strategy
        self.settings = settings
        self.market_data_service = market_data_service
        self.accounting_service = accounting_service
        self.state_store = state_store

    def generate(self, cycle_id: str, symbols: list[str], balances: list) -> list[Intent]:
        orderbooks: dict[str, tuple[Decimal, Decimal]] = {}
        get_best_bid_ask = getattr(self.market_data_service, "get_best_bid_ask", None)
        if callable(get_best_bid_ask):
            for symbol in symbols:
                pair = get_best_bid_ask(symbol)
                orderbooks[canonical_symbol(symbol)] = (
                    Decimal(str(pair[0])),
                    Decimal(str(pair[1])),
                )
        else:
            get_best_bids = getattr(self.market_data_service, "get_best_bids", None)
            best_bids = get_best_bids(symbols) if callable(get_best_bids) else {}
            for symbol in symbols:
                canonical = canonical_symbol(symbol)
                bid_value = best_bids.get(canonical, best_bids.get(symbol, 0))
                bid = Decimal(str(bid_value))
                orderbooks[canonical] = (bid, bid)
        positions = {p.symbol: p for p in self.accounting_service.get_positions()}
        open_orders: dict[str, int] = {}
        canonical_symbols = [canonical_symbol(s) for s in symbols]
        find_open_or_unknown_orders = getattr(self.state_store, "find_open_or_unknown_orders", None)
        if callable(find_open_or_unknown_orders):
            try:
                existing_orders = find_open_or_unknown_orders(
                    canonical_symbols,
                    new_grace_seconds=PENDING_GRACE_SECONDS,
                    include_new_after_grace=False,
                    include_escalated_unknown=False,
                )
            except TypeError:
                existing_orders = find_open_or_unknown_orders(canonical_symbols)
        else:
            existing_orders = []
        for order in existing_orders:
            open_orders[order.symbol] = open_orders.get(order.symbol, 0) + 1

        context = StrategyContext(
            cycle_id=cycle_id,
            symbols=symbols,
            orderbooks=orderbooks,
            positions=positions,
            balances=balances,
            open_orders=open_orders,
            settings=self.settings,
        )
        return self.strategy.generate_intents(context)
