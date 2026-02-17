from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import Balance, normalize_symbol
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.portfolio_policy_models import PortfolioPlan
from btcbot.domain.risk_budget import Mode
from btcbot.planning_kernel import (
    Allocator,
    Intent,
    OpenOrderView,
    OrderIntentBuilder,
    PlanningContext,
    StrategyEngine,
    UniverseSelector,
)
from btcbot.services.order_builder_service import OrderBuilderService
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.universe_selection_service import UniverseSelectionService


@dataclass(frozen=True)
class Stage7MarketDataSnapshot:
    mark_prices_try: Mapping[str, Decimal]
    symbol_rules: Mapping[str, Mapping[str, Decimal | str | int]]


@dataclass(frozen=True)
class Stage7PortfolioState:
    cash_try: Decimal
    positions_qty: Mapping[str, Decimal]
    open_orders: tuple[OpenOrderView, ...]
    balances: tuple[Balance, ...]


def normalize_stage4_open_orders(raw_orders: list[object]) -> list[OpenOrderView]:
    """Convert Stage4 order rows to minimal planning open-order view."""

    views: list[OpenOrderView] = []
    for item in raw_orders:
        symbol = normalize_symbol(getattr(item, "symbol", ""))
        side = str(getattr(item, "side", "")).upper()
        order_type = str(getattr(item, "type", getattr(item, "order_type", "LIMIT"))).upper()
        price = Decimal(str(getattr(item, "price", Decimal("0"))))
        qty = Decimal(str(getattr(item, "qty", Decimal("0"))))
        client_order_id = str(getattr(item, "client_order_id", ""))
        status = getattr(item, "status", None)
        if not symbol or not client_order_id:
            continue
        views.append(
            OpenOrderView(
                symbol=symbol,
                side=side,
                order_type=order_type,
                price=price,
                qty=qty,
                client_order_id=client_order_id,
                status=(str(status) if status is not None else None),
            )
        )
    return sorted(
        views,
        key=lambda item: (item.symbol, item.side, item.order_type, item.client_order_id),
    )


@dataclass
class Stage7UniverseSelectorAdapter(UniverseSelector):
    service: UniverseSelectionService
    exchange: object
    settings: Settings
    now_utc: datetime
    _cached: list[str] | None = None

    def prime(self) -> list[str]:
        if self._cached is None:
            result = self.service.select_universe(
                exchange=self.exchange,
                settings=self.settings,
                now_utc=self.now_utc,
            )
            self._cached = [normalize_symbol(symbol) for symbol in result.selected_symbols]
        return list(self._cached)

    def select(self, context: PlanningContext) -> list[str]:
        del context
        return self.prime()


@dataclass
class Stage7PortfolioStrategyAdapter(StrategyEngine):
    policy_service: PortfolioPolicyService
    settings: Settings
    now_utc: datetime
    final_mode: Mode
    _last_portfolio_plan: PortfolioPlan | None = None

    @property
    def last_portfolio_plan(self) -> PortfolioPlan:
        if self._last_portfolio_plan is None:
            raise RuntimeError("portfolio plan has not been computed")
        return self._last_portfolio_plan

    def generate_intents(self, context: PlanningContext, universe: list[str]) -> list[Intent]:
        portfolio = context.portfolio
        balances = list(getattr(portfolio, "balances", ()))
        plan = self.policy_service.build_plan(
            universe=list(universe),
            mark_prices_try=dict(context.market_data.mark_prices_try),
            balances=balances,
            settings=self.settings,
            now_utc=self.now_utc,
            final_mode=self.final_mode,
        )
        self._last_portfolio_plan = plan
        return [
            Intent(
                symbol=normalize_symbol(action.symbol),
                side=str(action.side).upper(),
                target_notional_try=Decimal(str(action.target_notional_try)),
                rationale=action.reason,
                strategy_id="stage7_portfolio_policy",
            )
            for action in plan.actions
        ]


@dataclass(frozen=True)
class Stage7PassThroughAllocator(Allocator):
    def allocate(self, context: PlanningContext, intents: list[Intent]) -> list[Intent]:
        del context
        return list(intents)


@dataclass
class Stage7OrderIntentBuilderAdapter(OrderIntentBuilder):
    order_builder: OrderBuilderService
    strategy_adapter: Stage7PortfolioStrategyAdapter
    settings: Settings
    final_mode: Mode
    now_utc: datetime
    rules: object
    rules_unavailable: Mapping[str, str]

    @staticmethod
    def normalize_side(raw: str) -> str | None:
        side = str(raw).strip().upper()
        if side in {"BUY", "SELL"}:
            return side
        return None

    @staticmethod
    def normalize_order_type(raw: str) -> str | None:
        order_type = str(raw).strip().upper()
        if order_type in {"LIMIT", "MARKET"}:
            return order_type
        return None

    def build(self, context: PlanningContext, intents: list[Intent]) -> list[OrderIntent]:
        del intents
        plan = self.strategy_adapter.last_portfolio_plan
        built = self.order_builder.build_intents(
            cycle_id=context.cycle_id,
            plan=plan,
            mark_prices_try=dict(context.market_data.mark_prices_try),
            rules=self.rules,
            settings=self.settings,
            final_mode=self.final_mode,
            now_utc=self.now_utc,
            rules_unavailable=dict(self.rules_unavailable),
        )
        normalized: list[OrderIntent] = []
        for item in built:
            normalized_symbol = normalize_symbol(item.symbol)
            normalized_side = self.normalize_side(item.side)
            normalized_order_type = self.normalize_order_type(item.order_type)
            invalid_reasons: list[str] = []
            if normalized_side is None:
                invalid_reasons.append("invalid_side")
            if normalized_order_type is None:
                invalid_reasons.append("invalid_order_type")
            if invalid_reasons:
                normalized.append(
                    OrderIntent(
                        cycle_id=item.cycle_id,
                        symbol=normalized_symbol,
                        side="BUY",
                        order_type="LIMIT",
                        price_try=Decimal("0"),
                        qty=Decimal("0"),
                        notional_try=Decimal("0"),
                        client_order_id=item.client_order_id,
                        reason=item.reason,
                        constraints_applied={
                            **dict(item.constraints_applied),
                            "normalization_error": ",".join(invalid_reasons),
                        },
                        skipped=True,
                        skip_reason="invalid_normalized_fields",
                    )
                )
                continue

            normalized.append(
                OrderIntent(
                    cycle_id=item.cycle_id,
                    symbol=normalized_symbol,
                    side=normalized_side,
                    order_type=normalized_order_type,
                    price_try=item.price_try,
                    qty=item.qty,
                    notional_try=item.notional_try,
                    client_order_id=item.client_order_id,
                    reason=item.reason,
                    constraints_applied=dict(item.constraints_applied),
                    skipped=item.skipped,
                    skip_reason=item.skip_reason,
                )
            )
        return normalized


def build_stage7_planning_context(
    *,
    cycle_id: str,
    now_utc: datetime,
    mark_prices: Mapping[str, Decimal],
    balances: list[Balance],
    open_orders: list[OpenOrderView],
    quote_ccy: str,
) -> PlanningContext:
    quote_asset = str(quote_ccy).upper()
    cash_try = Decimal("0")
    positions_qty: dict[str, Decimal] = {}
    for balance in balances:
        asset = str(balance.asset).upper()
        amount = Decimal(str(balance.free))
        if asset == quote_asset:
            cash_try = amount
            continue
        positions_qty[asset] = amount

    market = Stage7MarketDataSnapshot(
        mark_prices_try={
            normalize_symbol(symbol): Decimal(str(price)) for symbol, price in mark_prices.items()
        },
        symbol_rules={},
    )
    portfolio = Stage7PortfolioState(
        cash_try=cash_try,
        positions_qty=positions_qty,
        open_orders=tuple(open_orders),
        balances=tuple(balances),
    )
    return PlanningContext(
        cycle_id=cycle_id,
        now_utc=now_utc,
        market_data=market,
        portfolio=portfolio,
    )
