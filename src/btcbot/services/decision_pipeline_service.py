from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.action_to_order import build_exchange_rules, sized_action_to_order
from btcbot.config import Settings
from btcbot.domain.allocation import AllocationDecision, SizedAction
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import Order, Quantizer
from btcbot.domain.strategy_core import (
    Intent,
    OpenOrdersSummary,
    OrderBookSummary,
    PositionSummary,
    StrategyContext,
    StrategyKnobs,
)
from btcbot.domain.symbols import canonical_symbol
from btcbot.domain.universe_models import SymbolInfo
from btcbot.observability_decisions import emit_decision
from btcbot.services.allocation_service import AllocationKnobs, AllocationService
from btcbot.services.universe_service import select_universe
from btcbot.strategies.baseline_mean_reversion import BaselineMeanReversionStrategy
from btcbot.strategies.stage5_core import StrategyRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleDecisionReport:
    selected_universe: tuple[str, ...]
    intents: tuple[Intent, ...]
    allocation_actions: tuple[SizedAction, ...]
    allocation_decisions: tuple[AllocationDecision, ...]
    counters: Mapping[str, int]
    order_requests: tuple[Order, ...]
    deferred_order_requests: tuple[Order, ...]
    mapped_orders_count: int
    dropped_actions_count: int
    dropped_reasons: Mapping[str, int]
    cash_try: Decimal
    try_cash_target: Decimal
    investable_total_try: Decimal
    investable_this_cycle_try: Decimal
    deploy_budget_try: Decimal
    planned_total_try: Decimal
    unused_budget_try: Decimal
    investable_usage_reason: str


class DecisionPipelineService:
    def __init__(
        self,
        *,
        settings: Settings,
        strategy_registry: StrategyRegistry | None = None,
        universe_selector: Callable[..., list[str]] = select_universe,
        allocation_service: type[AllocationService] = AllocationService,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.universe_selector = universe_selector
        self.allocation_service = allocation_service
        self.registry = strategy_registry or self._default_registry()
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    def run_cycle(
        self,
        *,
        cycle_id: str,
        balances: Mapping[str, Decimal],
        positions: Mapping[str, PositionSummary],
        mark_prices: Mapping[str, Decimal],
        open_orders: list[Order],
        pair_info: list[PairInfo] | None,
        orderbooks: Mapping[str, OrderBookSummary] | None = None,
        bootstrap_enabled: bool,
        live_mode: bool,
        preferred_symbols: list[str] | None = None,
        aggressive_scores: Mapping[str, Decimal] | None = None,
        budget_notional_multiplier: Decimal = Decimal("1"),
    ) -> CycleDecisionReport:
        now_ts = self.now_provider()
        selected_universe = self._select_universe(
            pair_info=pair_info,
            mark_prices=mark_prices,
            fallback_symbols=tuple(sorted(mark_prices.keys())),
            preferred_symbols=preferred_symbols,
        )

        if aggressive_scores:
            report = self._run_aggressive_path(
                cycle_id=cycle_id,
                selected_universe=selected_universe,
                aggressive_scores=aggressive_scores,
                balances=balances,
                mark_prices=mark_prices,
                pair_info=pair_info,
                now_ts=now_ts,
                live_mode=live_mode,
                budget_notional_multiplier=budget_notional_multiplier,
            )
            self._log_report(report)
            return report

        intents = self._generate_intents(
            symbols=selected_universe,
            balances=balances,
            positions=positions,
            mark_prices=mark_prices,
            open_orders=open_orders,
            orderbooks=orderbooks,
            bootstrap_enabled=bootstrap_enabled,
            now_ts=now_ts,
        )

        allocation = self.allocation_service.allocate(
            intents=list(intents),
            balances=balances,
            positions=positions,
            mark_prices=mark_prices,
            knobs=AllocationKnobs(
                target_try_cash=self._to_decimal(self.settings.try_cash_target),
                try_cash_max=self._to_decimal(self.settings.try_cash_max),
                min_order_notional_try=self._to_decimal(self.settings.min_order_notional_try),
                fee_buffer_bps=self._to_decimal(self.settings.allocation_fee_buffer_bps),
                fee_buffer_ratio=self._to_decimal(self.settings.fee_buffer_ratio),
                max_intent_notional_try=(
                    self._to_decimal(self.settings.stage5_max_intent_notional_try)
                    * max(Decimal("0"), budget_notional_multiplier)
                ),
                max_position_try_per_symbol=self._to_decimal(
                    self.settings.max_position_try_per_symbol
                ),
                max_total_notional_try_per_cycle=(
                    self._to_decimal(self.settings.notional_cap_try_per_cycle)
                    * max(Decimal("0"), budget_notional_multiplier)
                ),
                investable_usage_mode=str(self.settings.investable_usage_mode),
                investable_usage_fraction=self._to_decimal(self.settings.investable_usage_fraction),
                max_try_per_cycle=(
                    self._to_decimal(self.settings.max_try_per_cycle)
                    * max(Decimal("0"), budget_notional_multiplier)
                ),
            ),
        )

        pair_info_by_symbol = {
            canonical_symbol(item.pair_symbol): item for item in (pair_info or [])
        }
        dropped_reasons: dict[str, int] = {}
        order_requests: list[Order] = []
        for action in allocation.actions:
            symbol = canonical_symbol(action.symbol)
            pair = pair_info_by_symbol.get(symbol)
            if pair is None:
                dropped_reasons["dropped_missing_pair_info"] = (
                    dropped_reasons.get("dropped_missing_pair_info", 0) + 1
                )
                continue
            order, drop_reason = sized_action_to_order(
                action,
                mode=("live" if live_mode else "dry_run"),
                mark_price=mark_prices.get(symbol),
                pair_info=pair,
                created_at=now_ts,
            )
            if order is None:
                key = drop_reason or "dropped_unknown"
                dropped_reasons[key] = dropped_reasons.get(key, 0) + 1
                continue
            order_requests.append(order)

        selected_orders, deferred_orders = self._select_orders_for_cycle(order_requests)

        self._emit_capital_gate_events(
            cycle_id=cycle_id,
            cash_try=allocation.cash_try,
            try_cash_target=allocation.try_cash_target,
            investable_total_try=allocation.investable_total_try,
            investable_this_cycle_try=allocation.investable_this_cycle_try,
        )

        report = CycleDecisionReport(
            selected_universe=tuple(selected_universe),
            intents=tuple(intents),
            allocation_actions=allocation.actions,
            allocation_decisions=allocation.decisions,
            counters=allocation.counters,
            order_requests=tuple(selected_orders),
            deferred_order_requests=tuple(deferred_orders),
            mapped_orders_count=len(order_requests),
            dropped_actions_count=sum(dropped_reasons.values()),
            dropped_reasons=dropped_reasons,
            cash_try=allocation.cash_try,
            try_cash_target=allocation.try_cash_target,
            investable_total_try=allocation.investable_total_try,
            investable_this_cycle_try=allocation.investable_this_cycle_try,
            deploy_budget_try=allocation.deploy_budget_try,
            planned_total_try=allocation.planned_total_try,
            unused_budget_try=allocation.unused_budget_try,
            investable_usage_reason=allocation.investable_usage_reason,
        )
        self._log_report(report)
        return report

    def _emit_capital_gate_events(
        self,
        *,
        cycle_id: str,
        cash_try: Decimal,
        try_cash_target: Decimal,
        investable_total_try: Decimal,
        investable_this_cycle_try: Decimal,
    ) -> None:
        if investable_total_try <= Decimal("0"):
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "capital_policy",
                    "reason_code": "capital_gate:cash_reserve_target",
                    "action": "BLOCK",
                    "payload": {
                        "cash_try": str(cash_try),
                        "try_cash_target": str(try_cash_target),
                        "investable_try": str(investable_total_try),
                    },
                },
            )
            return
        if investable_this_cycle_try <= Decimal("0"):
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "capital_policy",
                    "reason_code": "capital_gate:insufficient_investable_cash",
                    "action": "SUPPRESS",
                    "payload": {
                        "investable_total_try": str(investable_total_try),
                        "investable_this_cycle_try": str(investable_this_cycle_try),
                    },
                },
            )

    def _run_aggressive_path(
        self,
        *,
        cycle_id: str,
        selected_universe: list[str],
        aggressive_scores: Mapping[str, Decimal],
        balances: Mapping[str, Decimal],
        mark_prices: Mapping[str, Decimal],
        pair_info: list[PairInfo] | None,
        now_ts: datetime,
        live_mode: bool,
        budget_notional_multiplier: Decimal,
    ) -> CycleDecisionReport:
        cash_try = self._to_decimal(balances.get("TRY", Decimal("0")))
        try_cash_target = self._to_decimal(self.settings.try_cash_target)
        investable_total_try = max(Decimal("0"), cash_try - try_cash_target)
        scaled_multiplier = max(Decimal("0"), budget_notional_multiplier)
        investable_total_try = investable_total_try * scaled_multiplier
        fee_buffer_ratio = self._resolve_fee_buffer_ratio()
        deploy_budget_try = investable_total_try / (Decimal("1") + fee_buffer_ratio)

        pair_info_by_symbol = {
            canonical_symbol(item.pair_symbol): item for item in (pair_info or [])
        }
        eligible = [
            symbol
            for symbol in selected_universe
            if symbol in pair_info_by_symbol and mark_prices.get(symbol, Decimal("0")) > 0
        ]

        weight_scores = {
            symbol: max(Decimal("0"), self._to_decimal(aggressive_scores.get(symbol, Decimal("0"))))
            for symbol in eligible
        }

        kept = list(eligible)
        plans: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        dropped_reasons: dict[str, int] = {}

        while kept:
            total_weight = sum(weight_scores[symbol] for symbol in kept)
            equal_weight = Decimal("1") / Decimal(len(kept)) if kept else Decimal("0")
            dropped_any = False
            plans.clear()

            for symbol in list(kept):
                mark = self._to_decimal(mark_prices[symbol])
                pair = pair_info_by_symbol[symbol]
                rules = build_exchange_rules(pair)
                weight = weight_scores[symbol] / total_weight if total_weight > 0 else equal_weight
                notional_try = deploy_budget_try * weight
                price_q = Quantizer.quantize_price(mark, rules)
                if price_q <= 0:
                    kept.remove(symbol)
                    dropped_reasons["dropped_invalid_price"] = (
                        dropped_reasons.get("dropped_invalid_price", 0) + 1
                    )
                    dropped_any = True
                    continue
                qty_raw = notional_try / price_q if price_q > 0 else Decimal("0")
                if qty_raw <= 0:
                    kept.remove(symbol)
                    dropped_reasons["dropped_zero_qty"] = (
                        dropped_reasons.get("dropped_zero_qty", 0) + 1
                    )
                    dropped_any = True
                    continue
                qty_q = Quantizer.quantize_qty(qty_raw, rules)
                notional_q = qty_q * price_q
                if qty_q <= 0 or not Quantizer.validate_min_notional(price_q, qty_q, rules):
                    kept.remove(symbol)
                    dropped_reasons["dropped_min_notional"] = (
                        dropped_reasons.get("dropped_min_notional", 0) + 1
                    )
                    dropped_any = True
                    continue
                plans[symbol] = (price_q, qty_q, notional_q)

            if not dropped_any:
                break

        order_requests: list[Order] = []
        intents: list[Intent] = []
        actions: list[SizedAction] = []
        decisions: list[AllocationDecision] = []
        for index, symbol in enumerate(sorted(plans.keys())):
            price_q, qty_q, notional_q = plans[symbol]
            intents.append(
                Intent(
                    symbol=symbol,
                    side="buy",
                    intent_type="place",
                    target_notional_try=notional_q,
                    rationale="aggressive_24h_momentum",
                    strategy_id="aggressive_24h_momentum",
                )
            )
            actions.append(
                SizedAction(
                    symbol=symbol,
                    side="buy",
                    notional_try=notional_q,
                    qty=qty_q,
                    rationale="allocation:aggressive_24h_momentum",
                    strategy_id="aggressive_24h_momentum",
                    intent_index=index,
                )
            )
            decisions.append(
                AllocationDecision(
                    symbol=symbol,
                    side="buy",
                    intent_type="place",
                    requested_notional_try=notional_q,
                    allocated_notional_try=notional_q,
                    allocated_qty=qty_q,
                    status="accepted",
                    reason="ok",
                    strategy_id="aggressive_24h_momentum",
                    intent_index=index,
                )
            )
            order_requests.append(
                Order(
                    symbol=symbol,
                    side="buy",
                    type="limit",
                    price=price_q,
                    qty=qty_q,
                    status="new",
                    created_at=now_ts,
                    updated_at=now_ts,
                    client_order_id=f"s4a-{symbol.lower()}-{index}",
                    mode=("live" if live_mode else "dry_run"),
                )
            )

        planned_total_try = sum(item[2] for item in plans.values())
        unused_budget_try = max(Decimal("0"), deploy_budget_try - planned_total_try)
        counters = {"accepted": len(decisions)}

        selected_orders, deferred_orders = self._select_orders_for_cycle(order_requests)
        self._emit_capital_gate_events(
            cycle_id=cycle_id,
            cash_try=cash_try,
            try_cash_target=try_cash_target,
            investable_total_try=investable_total_try,
            investable_this_cycle_try=investable_total_try,
        )

        report = CycleDecisionReport(
            selected_universe=tuple(selected_universe),
            intents=tuple(intents),
            allocation_actions=tuple(actions),
            allocation_decisions=tuple(decisions),
            counters=counters,
            order_requests=tuple(selected_orders),
            deferred_order_requests=tuple(deferred_orders),
            mapped_orders_count=len(order_requests),
            dropped_actions_count=sum(dropped_reasons.values()),
            dropped_reasons=dropped_reasons,
            cash_try=cash_try,
            try_cash_target=try_cash_target,
            investable_total_try=investable_total_try,
            investable_this_cycle_try=investable_total_try,
            deploy_budget_try=deploy_budget_try,
            planned_total_try=planned_total_try,
            unused_budget_try=unused_budget_try,
            investable_usage_reason="use_all",
        )
        return report

    def _select_orders_for_cycle(
        self, order_requests: list[Order]
    ) -> tuple[list[Order], list[Order]]:
        max_orders = max(0, int(self.settings.max_orders_per_cycle))
        ranked = sorted(
            order_requests,
            key=lambda item: (
                Decimal("0") - item.qty * item.price,
                canonical_symbol(item.symbol),
                item.side,
                item.client_order_id or "",
            ),
        )
        return ranked[:max_orders], ranked[max_orders:]

    def _default_registry(self) -> StrategyRegistry:
        registry = StrategyRegistry()
        registry.register(BaselineMeanReversionStrategy())
        return registry

    def _select_universe(
        self,
        *,
        pair_info: list[PairInfo] | None,
        mark_prices: Mapping[str, Decimal],
        fallback_symbols: tuple[str, ...],
        preferred_symbols: list[str] | None,
    ) -> list[str]:
        if preferred_symbols:
            return [canonical_symbol(symbol) for symbol in preferred_symbols]

        configured_symbols = [canonical_symbol(symbol) for symbol in self.settings.symbols]
        if configured_symbols:
            return configured_symbols

        symbols: list[SymbolInfo]
        if pair_info:
            symbols = [SymbolInfo.from_pair_info(item) for item in pair_info]
        else:
            symbols = [
                SymbolInfo(symbol=symbol, base=None, quote="TRY", active=True)
                for symbol in fallback_symbols
            ]
        selected = self.universe_selector(
            symbols=symbols,
            orderbooks=self._to_orderbooks(mark_prices),
            knobs=self.settings.universe_knobs(),
        )
        if selected:
            return self._apply_portfolio_targets(selected)
        return self._apply_portfolio_targets(list(fallback_symbols))

    def _generate_intents(
        self,
        *,
        symbols: list[str],
        balances: Mapping[str, Decimal],
        positions: Mapping[str, PositionSummary],
        mark_prices: Mapping[str, Decimal],
        open_orders: list[Order],
        orderbooks: Mapping[str, OrderBookSummary] | None,
        bootstrap_enabled: bool,
        now_ts: datetime,
    ) -> list[Intent]:
        if not bootstrap_enabled:
            return []

        orders_summary: dict[str, OpenOrdersSummary] = defaultdict(OpenOrdersSummary)
        for order in open_orders:
            current = orders_summary[order.symbol]
            if order.side == "buy":
                orders_summary[order.symbol] = OpenOrdersSummary(
                    buy_count=current.buy_count + 1,
                    sell_count=current.sell_count,
                )
            else:
                orders_summary[order.symbol] = OpenOrdersSummary(
                    buy_count=current.buy_count,
                    sell_count=current.sell_count + 1,
                )

        intents: list[Intent] = []
        missing_orderbook_symbols: list[str] = []
        for symbol in sorted(symbols):
            mark = mark_prices.get(symbol)
            if mark is None or mark <= Decimal("0"):
                continue
            orderbook = (orderbooks or {}).get(symbol)
            if orderbook is None:
                missing_orderbook_symbols.append(symbol)
                orderbook = OrderBookSummary(best_bid=mark, best_ask=mark)
            context = StrategyContext(
                timestamp=now_ts,
                symbol=symbol,
                mark_price=mark,
                orderbook=orderbook,
                balances=balances,
                position=positions.get(symbol),
                open_orders=orders_summary.get(symbol, OpenOrdersSummary()),
                knobs=StrategyKnobs(
                    max_notional_try=self._to_decimal(self.settings.stage5_max_intent_notional_try),
                    bootstrap_notional_try=self._to_decimal(
                        self.settings.stage5_bootstrap_notional_try
                    ),
                ),
            )
            intents.extend(self.registry.generate_intents(context))
        if missing_orderbook_symbols:
            logger.info(
                "stage5_missing_orderbook_mark_fallback",
                extra={
                    "extra": {
                        "symbols": sorted(missing_orderbook_symbols),
                        "reason_code": "orderbook_unavailable_mark_fallback",
                    }
                },
            )
        return intents

    def _to_orderbooks(self, mark_prices: Mapping[str, Decimal]) -> dict[str, OrderBookSummary]:
        return {
            symbol: OrderBookSummary(best_bid=mark, best_ask=mark)
            for symbol, mark in mark_prices.items()
            if mark > Decimal("0")
        }

    def _log_report(self, report: CycleDecisionReport) -> None:
        logger.info(
            "stage5_cycle_decision_summary",
            extra={
                "extra": {
                    "universe_size": len(report.selected_universe),
                    "intent_count": len(report.intents),
                    "actions_count": len(report.allocation_actions),
                    "mapped_orders_count": report.mapped_orders_count,
                    "selected_orders_count": len(report.order_requests),
                    "deferred_orders_count": len(report.deferred_order_requests),
                    "dropped_actions_count": report.dropped_actions_count,
                    "dropped_reasons": dict(report.dropped_reasons),
                    "counters": dict(report.counters),
                }
            },
        )
        logger.debug(
            "stage5_cycle_decision_details",
            extra={
                "extra": {
                    "actions": [
                        {
                            "symbol": item.symbol,
                            "side": item.side,
                            "qty": str(item.qty),
                            "notional_try": str(item.notional_try),
                            "strategy_id": item.strategy_id,
                            "intent_index": item.intent_index,
                            "rationale": item.rationale,
                        }
                        for item in report.allocation_actions
                    ],
                    "decisions": [
                        {
                            "symbol": item.symbol,
                            "side": item.side,
                            "status": item.status,
                            "reason": item.reason,
                            "strategy_id": item.strategy_id,
                            "intent_index": item.intent_index,
                            "diagnostics": dict(item.diagnostics) if item.diagnostics else None,
                        }
                        for item in report.allocation_decisions
                    ],
                }
            },
        )

    def _apply_portfolio_targets(self, selected: list[str]) -> list[str]:
        raw = self.settings.portfolio_targets
        if not raw:
            return selected
        token = str(raw).strip().lower()
        if token.startswith("max_n="):
            try:
                max_n = int(token.split("=", 1)[1])
            except ValueError:
                return selected
            if max_n > 0:
                return selected[:max_n]
        return selected

    @staticmethod
    def _to_decimal(value: Decimal | float | int | str) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def _resolve_fee_buffer_ratio(self) -> Decimal:
        fee_buffer_ratio = self._to_decimal(self.settings.fee_buffer_ratio)
        if fee_buffer_ratio > Decimal("0"):
            return fee_buffer_ratio
        buffer_bps = max(self._to_decimal(self.settings.allocation_fee_buffer_bps), Decimal("0"))
        return buffer_bps / Decimal("10000")
