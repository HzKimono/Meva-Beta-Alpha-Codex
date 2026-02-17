from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import PairInfo, normalize_symbol
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.stage4 import Order, Position
from btcbot.domain.strategy_core import PositionSummary
from btcbot.planning_kernel import (
    Allocator,
    Intent,
    OpenOrderView,
    OrderIntentBuilder,
    Plan,
    PlanningContext,
    PlanningKernel,
    StrategyEngine,
    UniverseSelector,
)
from btcbot.services.decision_pipeline_service import CycleDecisionReport, DecisionPipelineService


@dataclass(frozen=True)
class Stage4MarketDataSnapshot:
    mark_prices_try: Mapping[str, Decimal]
    symbol_rules: Mapping[str, Mapping[str, Decimal | str | int]]


@dataclass(frozen=True)
class Stage4PortfolioState:
    cash_try: Decimal
    positions_qty: Mapping[str, Decimal]
    open_orders: tuple[OpenOrderView, ...]
    balances_try: Mapping[str, Decimal]
    positions_summary: Mapping[str, PositionSummary]


@dataclass(frozen=True)
class Stage4PlanningContext(PlanningContext):
    pair_info: tuple[PairInfo, ...] = ()
    live_mode: bool = False
    bootstrap_enabled: bool = True
    aggressive_scores: Mapping[str, Decimal] | None = None


@dataclass(frozen=True)
class Stage4KernelPlanningResult:
    plan: Plan
    decision_report: CycleDecisionReport
    bootstrap_drop_reasons: Mapping[str, int]


def normalize_stage4_open_orders(
    raw_orders: list[object], *, on_invalid: str = "skip"
) -> list[OpenOrderView]:
    """Normalize Stage4 open order rows into OpenOrderView.

    on_invalid:
      - "skip": skip malformed rows
      - "raise": raise ValueError for malformed side/order type
    """

    if on_invalid not in {"skip", "raise"}:
        raise ValueError("on_invalid must be 'skip' or 'raise'")

    views: list[OpenOrderView] = []
    for item in raw_orders:
        symbol = normalize_symbol(getattr(item, "symbol", ""))
        side_raw = str(getattr(item, "side", "")).upper()
        order_type_raw = str(getattr(item, "type", getattr(item, "order_type", ""))).upper()
        client_order_id = str(getattr(item, "client_order_id", "")).strip()
        if not symbol or not client_order_id:
            continue

        if side_raw not in {"BUY", "SELL"}:
            if on_invalid == "raise":
                raise ValueError(f"invalid open-order side: {side_raw}")
            continue
        if order_type_raw not in {"LIMIT", "MARKET"}:
            if on_invalid == "raise":
                raise ValueError(f"invalid open-order type: {order_type_raw}")
            continue

        price = Decimal(str(getattr(item, "price", Decimal("0"))))
        qty = Decimal(str(getattr(item, "qty", Decimal("0"))))
        status = getattr(item, "status", None)
        views.append(
            OpenOrderView(
                symbol=symbol,
                side=side_raw,
                order_type=order_type_raw,
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


@dataclass(frozen=True)
class Stage4UniverseSelectorAdapter(UniverseSelector):
    selected_symbols: tuple[str, ...]

    def select(self, context: PlanningContext) -> list[str]:
        del context
        return list(self.selected_symbols)


@dataclass
class Stage4DecisionStrategyAdapter(StrategyEngine):
    decision_pipeline: DecisionPipelineService
    _last_decision_report: CycleDecisionReport | None = None

    @property
    def last_decision_report(self) -> CycleDecisionReport:
        if self._last_decision_report is None:
            raise RuntimeError("stage4 decision report has not been generated")
        return self._last_decision_report

    def generate_intents(self, context: PlanningContext, universe: list[str]) -> list[Intent]:
        if not isinstance(context, Stage4PlanningContext):
            raise TypeError("Stage4DecisionStrategyAdapter requires Stage4PlanningContext")
        portfolio = context.portfolio
        if not isinstance(portfolio, Stage4PortfolioState):
            raise TypeError("Stage4DecisionStrategyAdapter requires Stage4PortfolioState")

        report = self.decision_pipeline.run_cycle(
            cycle_id=context.cycle_id,
            balances=dict(portfolio.balances_try),
            positions=dict(portfolio.positions_summary),
            mark_prices=dict(context.market_data.mark_prices_try),
            open_orders=_open_orders_from_portfolio(context, portfolio),
            pair_info=list(context.pair_info),
            bootstrap_enabled=context.bootstrap_enabled,
            live_mode=context.live_mode,
            preferred_symbols=list(universe),
            aggressive_scores=(
                dict(context.aggressive_scores) if context.aggressive_scores is not None else None
            ),
        )
        self._last_decision_report = report
        return [
            Intent(
                symbol=normalize_symbol(item.symbol),
                side=str(item.side).upper(),
                target_notional_try=Decimal(str(item.target_notional_try)),
                rationale=item.rationale,
                strategy_id=item.strategy_id,
            )
            for item in report.intents
        ]


@dataclass(frozen=True)
class Stage4PassThroughAllocator(Allocator):
    def allocate(self, context: PlanningContext, intents: list[Intent]) -> list[Intent]:
        del context
        return list(intents)


@dataclass
class Stage4CompatOrderIntentBuilderAdapter(OrderIntentBuilder):
    """Temporary compat adapter.

    TODO(stage4-kernel-cutover): remove this legacy-backed adapter once Stage4 planning
    natively emits OrderIntents without delegating to DecisionPipelineService/bootstrap path.
    """

    strategy_adapter: Stage4DecisionStrategyAdapter
    bootstrap_builder: object
    live_mode: bool

    _last_bootstrap_drop_reasons: Mapping[str, int] | None = None

    @property
    def last_bootstrap_drop_reasons(self) -> Mapping[str, int]:
        return dict(self._last_bootstrap_drop_reasons or {})

    @staticmethod
    def normalize_side(raw: str) -> str | None:
        side = str(raw).strip().upper()
        if side in {"BUY", "SELL"}:
            return side
        return None

    @staticmethod
    def normalize_order_type(raw: str) -> str | None:
        order_type = str(raw).strip().upper()
        if order_type in {"LIMIT"}:
            return order_type
        return None

    def build(self, context: PlanningContext, intents: list[Intent]) -> list[OrderIntent]:
        del intents
        if not isinstance(context, Stage4PlanningContext):
            raise TypeError("Stage4CompatOrderIntentBuilderAdapter requires Stage4PlanningContext")
        report = self.strategy_adapter.last_decision_report

        pipeline_orders = list(report.order_requests)
        if pipeline_orders:
            selected_orders = pipeline_orders
            self._last_bootstrap_drop_reasons = {}
        else:
            bootstrap_orders, drop_reasons = self.bootstrap_builder(
                cycle_id=context.cycle_id,
                symbols=list(context.preferred_symbols),
                mark_prices=dict(context.market_data.mark_prices_try),
                try_cash=context.portfolio.cash_try,
                open_orders=_open_orders_from_context(context),
                live_mode=self.live_mode,
                bootstrap_enabled=context.bootstrap_enabled,
                pair_info=list(context.pair_info),
                now_utc=context.now_utc,
            )
            selected_orders = bootstrap_orders
            self._last_bootstrap_drop_reasons = dict(drop_reasons)

        normalized: list[OrderIntent] = []
        for order in selected_orders:
            normalized_symbol = normalize_symbol(order.symbol)
            normalized_side = self.normalize_side(order.side)
            normalized_order_type = self.normalize_order_type(order.type)
            if normalized_side is None or normalized_order_type is None:
                reason_bits: list[str] = []
                if normalized_side is None:
                    reason_bits.append("invalid_side")
                if normalized_order_type is None:
                    reason_bits.append("invalid_order_type")
                normalized.append(
                    OrderIntent(
                        cycle_id=context.cycle_id,
                        symbol=normalized_symbol,
                        side="BUY",
                        order_type="LIMIT",
                        price_try=Decimal("0"),
                        qty=Decimal("0"),
                        notional_try=Decimal("0"),
                        client_order_id=str(order.client_order_id or ""),
                        reason=str(getattr(order, "status", "new")),
                        constraints_applied={"normalization_error": ",".join(reason_bits)},
                        skipped=True,
                        skip_reason="invalid_normalized_fields",
                    )
                )
                continue

            normalized.append(
                OrderIntent(
                    cycle_id=context.cycle_id,
                    symbol=normalized_symbol,
                    side=normalized_side,
                    order_type=normalized_order_type,
                    price_try=Decimal(str(order.price)),
                    qty=Decimal(str(order.qty)),
                    notional_try=Decimal(str(order.price)) * Decimal(str(order.qty)),
                    client_order_id=str(order.client_order_id or ""),
                    reason=str(getattr(order, "status", "new")),
                    constraints_applied={},
                    skipped=False,
                    skip_reason=None,
                )
            )
        return normalized


def build_stage4_planning_context(
    *,
    cycle_id: str,
    now_utc: datetime,
    selected_symbols: list[str],
    mark_prices: Mapping[str, Decimal],
    try_cash: Decimal,
    positions: list[Position],
    open_orders: list[Order],
    pair_info: list[PairInfo] | None,
    live_mode: bool,
    bootstrap_enabled: bool,
    aggressive_scores: Mapping[str, Decimal] | None,
) -> Stage4PlanningContext:
    market = Stage4MarketDataSnapshot(
        mark_prices_try={
            normalize_symbol(symbol): Decimal(str(price)) for symbol, price in mark_prices.items()
        },
        symbol_rules={},
    )
    positions_qty = {normalize_symbol(pos.symbol): Decimal(str(pos.qty)) for pos in positions}
    positions_summary = {
        normalize_symbol(pos.symbol): PositionSummary(
            symbol=normalize_symbol(pos.symbol),
            qty=Decimal(str(pos.qty)),
            avg_cost=Decimal(str(pos.avg_cost_try)),
        )
        for pos in positions
    }
    portfolio = Stage4PortfolioState(
        cash_try=Decimal(str(try_cash)),
        positions_qty=positions_qty,
        open_orders=tuple(normalize_stage4_open_orders(open_orders, on_invalid="skip")),
        balances_try={"TRY": Decimal(str(try_cash))},
        positions_summary=positions_summary,
    )
    return Stage4PlanningContext(
        cycle_id=cycle_id,
        now_utc=now_utc.astimezone(UTC),
        market_data=market,
        portfolio=portfolio,
        preferred_symbols=tuple(normalize_symbol(symbol) for symbol in selected_symbols),
        pair_info=tuple(pair_info or []),
        live_mode=live_mode,
        bootstrap_enabled=bootstrap_enabled,
        aggressive_scores=(
            {normalize_symbol(k): Decimal(str(v)) for k, v in aggressive_scores.items()}
            if aggressive_scores is not None
            else None
        ),
    )


def build_stage4_kernel_plan(
    *,
    settings: Settings,
    cycle_id: str,
    now_utc: datetime,
    selected_symbols: list[str],
    mark_prices: Mapping[str, Decimal],
    try_cash: Decimal,
    positions: list[Position],
    open_orders: list[Order],
    pair_info: list[PairInfo] | None,
    live_mode: bool,
    aggressive_scores: Mapping[str, Decimal] | None,
    bootstrap_builder: object,
) -> Stage4KernelPlanningResult:
    context = build_stage4_planning_context(
        cycle_id=cycle_id,
        now_utc=now_utc,
        selected_symbols=selected_symbols,
        mark_prices=mark_prices,
        try_cash=try_cash,
        positions=positions,
        open_orders=open_orders,
        pair_info=pair_info,
        live_mode=live_mode,
        bootstrap_enabled=settings.stage4_bootstrap_intents,
        aggressive_scores=aggressive_scores,
    )
    strategy_adapter = Stage4DecisionStrategyAdapter(
        decision_pipeline=DecisionPipelineService(settings=settings)
    )
    order_builder_adapter = Stage4CompatOrderIntentBuilderAdapter(
        strategy_adapter=strategy_adapter,
        bootstrap_builder=bootstrap_builder,
        live_mode=live_mode,
    )
    kernel = PlanningKernel(
        universe_selector=Stage4UniverseSelectorAdapter(
            selected_symbols=tuple(context.preferred_symbols)
        ),
        strategy_engine=strategy_adapter,
        allocator=Stage4PassThroughAllocator(),
        order_intent_builder=order_builder_adapter,
    )
    plan = kernel.plan(context)
    return Stage4KernelPlanningResult(
        plan=plan,
        decision_report=strategy_adapter.last_decision_report,
        bootstrap_drop_reasons=dict(order_builder_adapter.last_bootstrap_drop_reasons),
    )


def _open_orders_from_context(context: Stage4PlanningContext) -> list[Order]:
    return _open_orders_from_portfolio(context, context.portfolio)


def _open_orders_from_portfolio(
    context: PlanningContext, portfolio: Stage4PortfolioState
) -> list[Order]:
    open_orders: list[Order] = []
    for item in portfolio.open_orders:
        open_orders.append(
            Order(
                symbol=item.symbol,
                side=item.side.lower(),
                type=item.order_type.lower(),
                price=item.price,
                qty=item.qty,
                status=str(item.status or "new"),
                created_at=context.now_utc,
                updated_at=context.now_utc,
                client_order_id=item.client_order_id,
            )
        )
    return open_orders
