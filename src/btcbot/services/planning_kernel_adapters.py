from __future__ import annotations

from dataclasses import dataclass

from btcbot.planning_kernel import ExecutionPort, OrderIntent, Plan


@dataclass(frozen=True)
class Stage4PlanConsumer:
    """Adapter glue for migrating Stage4 runner to the shared PlanningKernel."""

    execution: ExecutionPort

    def consume(self, plan: Plan) -> list[str]:
        """Submit non-skipped intents in deterministic order.

        TODO: Replace Stage4 lifecycle bootstrap/pipeline order construction with Plan usage.
        """

        submitted_ids: list[str] = []
        for intent in sorted(
            plan.order_intents,
            key=lambda item: (item.symbol, item.side, item.client_order_id),
        ):
            if intent.skipped:
                continue
            submitted_ids.append(self.execution.submit(intent))
        return submitted_ids


@dataclass(frozen=True)
class Stage7PlanConsumer:
    """Adapter glue for migrating Stage7 runner to the shared PlanningKernel."""

    execution: ExecutionPort

    def consume(self, plan: Plan) -> list[str]:
        """Submit non-skipped intents in deterministic order.

        TODO: Replace PortfolioPolicyService + OrderBuilderService output handoff with Plan usage.
        """

        submitted_ids: list[str] = []
        for intent in sorted(
            plan.order_intents,
            key=lambda item: (item.symbol, item.side, item.client_order_id),
        ):
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
