from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic, sleep


@dataclass(frozen=True)
class EndpointBudget:
    tokens_per_second: float
    burst_capacity: int

    def validate(self, *, label: str) -> None:
        if self.tokens_per_second <= 0:
            raise ValueError(f"{label} tokens_per_second must be > 0")
        if self.burst_capacity < 1:
            raise ValueError(f"{label} burst_capacity must be >= 1")


class TokenBucketRateLimiter:
    def __init__(
        self,
        default_budget: EndpointBudget,
        *,
        group_budgets: dict[str, EndpointBudget] | None = None,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        default_budget.validate(label="default_budget")
        for group, budget in (group_budgets or {}).items():
            budget.validate(label=f"group_budget[{group}]")
        self._clock = clock or monotonic
        self._sleep = sleep_fn or sleep
        self._lock = Lock()
        self._group_budgets = {"default": default_budget, **(group_budgets or {})}
        self._state = {
            group: {
                "tokens": float(budget.burst_capacity),
                "updated_at": self._clock(),
                "cooldown_until": 0.0,
            }
            for group, budget in self._group_budgets.items()
        }

    def _budget_for(self, group: str) -> EndpointBudget:
        return self._group_budgets.get(group, self._group_budgets["default"])

    def _state_for(self, group: str) -> dict[str, float]:
        state = self._state.get(group)
        if state is None:
            budget = self._budget_for(group)
            state = {
                "tokens": float(budget.burst_capacity),
                "updated_at": self._clock(),
                "cooldown_until": 0.0,
            }
            self._state[group] = state
        return state

    def _refill(self, group: str) -> None:
        state = self._state_for(group)
        budget = self._budget_for(group)
        now = self._clock()
        elapsed = max(0.0, now - state["updated_at"])
        if elapsed > 0:
            state["tokens"] = min(
                float(budget.burst_capacity),
                state["tokens"] + elapsed * budget.tokens_per_second,
            )
            state["updated_at"] = now

    def acquire(self, group: str, cost: int = 1) -> float:
        if cost < 1:
            return 0.0

        waited = 0.0
        while True:
            with self._lock:
                self._refill(group)
                state = self._state_for(group)
                budget = self._budget_for(group)
                now = self._clock()
                cooldown_wait = max(0.0, state["cooldown_until"] - now)
                if cooldown_wait > 0:
                    wait_seconds = cooldown_wait
                elif state["tokens"] >= cost:
                    state["tokens"] -= cost
                    return waited
                else:
                    deficit = float(cost) - state["tokens"]
                    wait_seconds = deficit / max(budget.tokens_per_second, 1e-9)
            wait_seconds = max(0.0, wait_seconds)
            waited += wait_seconds
            self._sleep(wait_seconds)

    def penalize_on_429(self, group: str, retry_after_s: float | None) -> None:
        with self._lock:
            state = self._state_for(group)
            state["tokens"] = 0.0
            budget = self._budget_for(group)
            if retry_after_s is not None and retry_after_s > 0:
                state["cooldown_until"] = max(state["cooldown_until"], self._clock() + retry_after_s)
                return

            fallback_cooldown = min(1.5, max(0.25, 1.0 / budget.tokens_per_second))
            state["cooldown_until"] = max(
                state["cooldown_until"],
                self._clock() + fallback_cooldown,
            )


def map_endpoint_group(path: str) -> str:
    normalized = path.lower()
    if "/orderbook" in normalized or "/ticker" in normalized or "/ohlc" in normalized:
        return "market_data"
    if "/order" in normalized and "/users/transactions" not in normalized:
        return "orders"
    if "/users/" in normalized or "/openorders" in normalized:
        return "account"
    return "market_data"
