from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from btcbot.accounting.accounting_service import AccountingService
from btcbot.config import Settings
from btcbot.domain.intent import Intent, build_idempotency_key
from btcbot.domain.models import OrderSide
from btcbot.domain.risk_budget import Mode
from btcbot.domain.symbols import canonical_symbol
from btcbot.services.market_data_service import MarketDataService
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.state_store import PENDING_GRACE_SECONDS, StateStore
from btcbot.strategies.base import Strategy
from btcbot.strategies.context import StrategyContext

logger = logging.getLogger(__name__)


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
        self.portfolio_policy_service = PortfolioPolicyService()

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
        strategy_intents = self.strategy.generate_intents(context)
        rebalance_intents = self._generate_portfolio_rebalance_intents(
            cycle_id=cycle_id,
            symbols=canonical_symbols,
            orderbooks=orderbooks,
            balances=balances,
        )
        return strategy_intents + rebalance_intents

    def _generate_portfolio_rebalance_intents(
        self,
        *,
        cycle_id: str,
        symbols: list[str],
        orderbooks: dict[str, tuple[Decimal, Decimal]],
        balances: list,
    ) -> list[Intent]:
        final_mode = self._resolve_final_mode()
        mark_prices_try: dict[str, Decimal] = {}
        for symbol in sorted(set(symbols)):
            bid_ask = orderbooks.get(symbol)
            if not bid_ask:
                continue
            bid, ask = bid_ask
            if bid > 0 and ask > 0:
                mark_prices_try[symbol] = (bid + ask) / Decimal("2")
            elif bid > 0:
                mark_prices_try[symbol] = bid
            elif ask > 0:
                mark_prices_try[symbol] = ask

        plan = self.portfolio_policy_service.build_plan(
            universe=sorted(set(symbols)),
            mark_prices_try=mark_prices_try,
            balances=balances,
            settings=self.settings,
            now_utc=datetime.now(UTC),
            final_mode=final_mode,
        )

        offset_bps = Decimal(str(self.settings.stage7_order_offset_bps))
        intents: list[Intent] = []
        for action in plan.actions:
            symbol = canonical_symbol(action.symbol)
            mark = mark_prices_try.get(symbol)
            if mark is None or mark <= 0:
                continue
            if action.side == "BUY":
                limit_price = mark * (Decimal("1") + (offset_bps / Decimal("10000")))
                side = OrderSide.BUY
            else:
                limit_price = mark * (Decimal("1") - (offset_bps / Decimal("10000")))
                side = OrderSide.SELL
            limit_price = limit_price.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            rounded_notional = action.target_notional_try.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            idempotency_key = build_idempotency_key(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                qty=rounded_notional,
                limit_price=limit_price,
            )
            intent = Intent(
                intent_id=f"{cycle_id}:{symbol}:{action.side}:{rounded_notional}",
                symbol=symbol,
                side=side,
                qty=action.est_qty,
                limit_price=limit_price,
                reason=f"portfolio_rebalance: {action.reason}",
                confidence=1.0,
                ttl_seconds=self.settings.ttl_seconds,
                idempotency_key=idempotency_key,
            )
            intents.append(intent)

        logger.info(
            "portfolio_rebalance_intents_generated",
            extra={
                "extra": {
                    "cycle_id": cycle_id,
                    "final_mode": final_mode.value,
                    "actions": len(plan.actions),
                    "intents": len(intents),
                    "planned_turnover_try": str(
                        sum(
                            (action.target_notional_try for action in plan.actions),
                            start=Decimal("0"),
                        )
                    ),
                }
            },
        )
        return intents

    def _resolve_final_mode(self) -> Mode:
        getter = getattr(self.state_store, "get_latest_risk_mode", None)
        if not callable(getter):
            return Mode.NORMAL
        try:
            mode = getter()
        except Exception:  # noqa: BLE001
            return Mode.NORMAL
        if isinstance(mode, Mode):
            return mode
        normalized = str(mode or "").strip().lower()
        for candidate in Mode:
            if candidate.value == normalized:
                return candidate
        return Mode.NORMAL
