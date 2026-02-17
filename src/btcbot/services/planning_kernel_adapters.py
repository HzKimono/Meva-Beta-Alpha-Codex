from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from btcbot.config import Settings
from btcbot.domain.order_intent import OrderIntent
from btcbot.planning_kernel import ExecutionPort, Plan
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.state_store import StateStore


@dataclass(frozen=True)
class Stage4PlanConsumer:
    """Adapter glue for migrating Stage4 runner to the shared PlanningKernel."""

    execution: ExecutionPort

    def consume(self, plan: Plan) -> list[str]:
        submitted_ids: list[str] = []
        for intent in plan.order_intents:
            if intent.skipped:
                continue
            submitted_ids.append(self.execution.submit(intent))
        return submitted_ids


@dataclass(frozen=True)
class Stage7PlanConsumer:
    """Adapter glue for Stage7 runner with shared PlanningKernel output."""

    execution: ExecutionPort

    def consume(self, plan: Plan) -> list[str]:
        submitted_ids: list[str] = []
        for intent in plan.order_intents:
            if intent.skipped:
                continue
            submitted_ids.append(self.execution.submit(intent))
        return submitted_ids


class InMemoryExecutionPort:
    """Test double implementing the shared execution interface."""

    def __init__(self) -> None:
        self._orders: dict[str, OrderIntent] = {}

    def submit(self, order_intent: OrderIntent) -> str:
        order_id = order_intent.client_order_id
        self._orders[order_id] = order_intent
        return order_id

    def cancel(self, order_id: str) -> None:
        self._orders.pop(order_id, None)

    def replace(self, order_id: str, new_order_intent: OrderIntent) -> str:
        self.cancel(order_id)
        return self.submit(new_order_intent)

    def reconcile(self) -> dict[str, object]:
        return {
            "open_order_count": len(self._orders),
            "order_ids": sorted(self._orders.keys()),
        }


class Stage7ExecutionPort:
    """ExecutionPort implementation backed by existing OMSService dry-run flow."""

    def __init__(
        self,
        *,
        cycle_id: str,
        now_utc: datetime,
        oms_service: OMSService,
        market_sim: Stage7MarketSimulator,
        state_store: StateStore,
        settings: Settings,
    ) -> None:
        self._cycle_id = cycle_id
        self._now_utc = now_utc
        self._oms_service = oms_service
        self._market_sim = market_sim
        self._state_store = state_store
        self._settings = settings
        self._pending_intents: dict[str, OrderIntent] = {}
        self._cancel_requests: set[str] = set()

    def submit(self, order_intent: OrderIntent) -> str:
        self._pending_intents[order_intent.client_order_id] = order_intent
        return order_intent.client_order_id

    def cancel(self, order_id: str) -> None:
        self._cancel_requests.add(order_id)

    def replace(self, order_id: str, new_order_intent: OrderIntent) -> str:
        self.cancel(order_id)
        return self.submit(new_order_intent)

    def reconcile(self) -> Mapping[str, object]:
        reconciled_orders, reconciled_events = self._oms_service.reconcile_open_orders(
            cycle_id=self._cycle_id,
            now_utc=self._now_utc,
            state_store=self._state_store,
            settings=self._settings,
            market_sim=self._market_sim,
        )

        intents = [
            self._pending_intents[key]
            for key in sorted(self._pending_intents.keys())
            if not self._pending_intents[key].skipped
        ]
        orders, events = self._oms_service.process_intents(
            cycle_id=self._cycle_id,
            now_utc=self._now_utc,
            intents=intents,
            market_sim=self._market_sim,
            state_store=self._state_store,
            settings=self._settings,
            cancel_requests=sorted(self._cancel_requests),
        )

        self._pending_intents.clear()
        self._cancel_requests.clear()

        merged_orders = [*reconciled_orders, *orders]
        merged_events = [*reconciled_events, *events]
        return {
            "orders": merged_orders,
            "events": merged_events,
            "submitted": len(orders),
        }
