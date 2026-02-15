from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.action_to_order import sized_action_to_order
from btcbot.config import Settings
from btcbot.domain.allocation import AllocationDecision, SizedAction
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import Order
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
    mapped_orders_count: int
    dropped_actions_count: int
    dropped_reasons: Mapping[str, int]


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
        bootstrap_enabled: bool,
        live_mode: bool,
    ) -> CycleDecisionReport:
        del cycle_id
        now_ts = self.now_provider()
        selected_universe = self._select_universe(
            pair_info=pair_info,
            mark_prices=mark_prices,
            fallback_symbols=tuple(sorted(mark_prices.keys())),
        )
        intents = self._generate_intents(
            symbols=selected_universe,
            balances=balances,
            positions=positions,
            mark_prices=mark_prices,
            open_orders=open_orders,
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
                min_order_notional_try=self._to_decimal(self.settings.min_order_notional_try),
                max_intent_notional_try=Decimal("0"),
                max_position_try_per_symbol=self._to_decimal(
                    self.settings.max_position_try_per_symbol
                ),
                max_total_notional_try_per_cycle=self._to_decimal(
                    self.settings.notional_cap_try_per_cycle
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

        report = CycleDecisionReport(
            selected_universe=tuple(selected_universe),
            intents=tuple(intents),
            allocation_actions=allocation.actions,
            allocation_decisions=allocation.decisions,
            counters=allocation.counters,
            order_requests=tuple(order_requests),
            mapped_orders_count=len(order_requests),
            dropped_actions_count=sum(dropped_reasons.values()),
            dropped_reasons=dropped_reasons,
        )
        self._log_report(report)
        return report

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
    ) -> list[str]:
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
        for symbol in sorted(symbols):
            mark = mark_prices.get(symbol)
            if mark is None or mark <= Decimal("0"):
                continue
            context = StrategyContext(
                timestamp=now_ts,
                symbol=symbol,
                mark_price=mark,
                orderbook=OrderBookSummary(best_bid=mark, best_ask=mark),
                balances=balances,
                position=positions.get(symbol),
                open_orders=orders_summary.get(symbol, OpenOrdersSummary()),
                knobs=StrategyKnobs(),
            )
            intents.extend(self.registry.generate_intents(context))
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
