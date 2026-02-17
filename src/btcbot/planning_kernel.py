from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from btcbot.domain.models import normalize_symbol
from btcbot.domain.order_intent import OrderIntent


@dataclass(frozen=True)
class OpenOrderView:
    symbol: str
    side: str
    order_type: str
    price: Decimal
    qty: Decimal
    client_order_id: str
    status: str | None = None


class MarketDataSnapshot(Protocol):
    """Read-only market data view used by deterministic planning."""

    @property
    def mark_prices_try(self) -> Mapping[str, Decimal]: ...

    @property
    def symbol_rules(self) -> Mapping[str, Mapping[str, Decimal | str | int]]: ...


class PortfolioState(Protocol):
    """Read-only portfolio/account state used by planning."""

    @property
    def cash_try(self) -> Decimal: ...

    @property
    def positions_qty(self) -> Mapping[str, Decimal]: ...

    @property
    def open_orders(self) -> Sequence[OpenOrderView]: ...


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: str
    target_notional_try: Decimal
    rationale: str
    strategy_id: str

    def normalized_side(self) -> str:
        return str(self.side).upper()


@dataclass(frozen=True)
class Plan:
    cycle_id: str
    generated_at: datetime
    universe: tuple[str, ...]
    intents_raw: tuple[Intent, ...]
    intents_allocated: tuple[Intent, ...]
    order_intents: tuple[OrderIntent, ...]
    planning_gates: Mapping[str, str]
    diagnostics: Mapping[str, str]


@dataclass(frozen=True)
class PlanningContext:
    cycle_id: str
    now_utc: datetime
    market_data: MarketDataSnapshot
    portfolio: PortfolioState
    preferred_symbols: tuple[str, ...] = ()


class UniverseSelector(Protocol):
    def select(self, context: PlanningContext) -> Sequence[str]: ...


class StrategyEngine(Protocol):
    def generate_intents(
        self, context: PlanningContext, universe: Sequence[str]
    ) -> Sequence[Intent]: ...


class Allocator(Protocol):
    def allocate(self, context: PlanningContext, intents: Sequence[Intent]) -> Sequence[Intent]: ...


class OrderIntentBuilder(Protocol):
    def build(
        self, context: PlanningContext, intents: Sequence[Intent]
    ) -> Sequence[OrderIntent]: ...


class PlanningKernelProtocol(Protocol):
    def plan(self, context: PlanningContext) -> Plan: ...


@dataclass(frozen=True)
class PlanningKernel:
    """Shared deterministic planning pipeline for Stage4 and Stage7."""

    universe_selector: UniverseSelector
    strategy_engine: StrategyEngine
    allocator: Allocator
    order_intent_builder: OrderIntentBuilder

    def plan(self, context: PlanningContext) -> Plan:
        """Build a deterministic plan consumed by execution layers.

        Determinism contract:
        - universe sorted by normalized symbol
        - intents sorted by (symbol, side, strategy_id, rationale, target_notional_try)
        - order_intents sorted by (symbol, side, client_order_id)
        """

        universe = tuple(
            sorted({normalize_symbol(symbol) for symbol in self.universe_selector.select(context)})
        )
        raw_intents = tuple(
            sorted(
                self.strategy_engine.generate_intents(context, universe),
                key=lambda item: (
                    normalize_symbol(item.symbol),
                    str(item.side).upper(),
                    str(item.strategy_id),
                    str(item.rationale),
                    Decimal(str(item.target_notional_try)),
                ),
            )
        )
        allocated_intents = tuple(
            sorted(
                self.allocator.allocate(context, raw_intents),
                key=lambda item: (
                    normalize_symbol(item.symbol),
                    str(item.side).upper(),
                    str(item.strategy_id),
                    str(item.rationale),
                    Decimal(str(item.target_notional_try)),
                ),
            )
        )
        order_intents = tuple(
            sorted(
                self.order_intent_builder.build(context, allocated_intents),
                key=lambda item: (
                    normalize_symbol(item.symbol),
                    str(item.side).upper(),
                    str(item.client_order_id),
                ),
            )
        )

        planning_gates = {
            "market_data_available": str(bool(context.market_data.mark_prices_try)).lower(),
            "cash_available": str(context.portfolio.cash_try > Decimal("0")).lower(),
            "orders_planned": str(any(not item.skipped for item in order_intents)).lower(),
        }
        diagnostics = {
            "universe_count": str(len(universe)),
            "raw_intent_count": str(len(raw_intents)),
            "allocated_intent_count": str(len(allocated_intents)),
            "order_intent_count": str(len(order_intents)),
            "open_order_count": str(len(context.portfolio.open_orders)),
        }

        return Plan(
            cycle_id=context.cycle_id,
            generated_at=context.now_utc.astimezone(UTC),
            universe=universe,
            intents_raw=raw_intents,
            intents_allocated=allocated_intents,
            order_intents=order_intents,
            planning_gates=planning_gates,
            diagnostics=diagnostics,
        )


class ExecutionPort(Protocol):
    def submit(self, order_intent: OrderIntent) -> str: ...

    def cancel(self, order_id: str) -> None: ...

    def replace(self, order_id: str, new_order_intent: OrderIntent) -> str: ...

    def reconcile(self) -> Mapping[str, object]: ...
