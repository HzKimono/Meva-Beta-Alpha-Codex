from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
        budget: EndpointBudget | None = None,
        *,
        rate_per_sec: float | None = None,
        burst: int | None = None,
        time_source: Callable[[], float] | None = None,
        rps: float | None = None,
        group_budgets: dict[str, EndpointBudget] | None = None,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
        rate_per_sec: float | None = None,
        burst: int | None = None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if budget is None:
            if rate_per_sec is None or burst is None:
                raise TypeError(
                    "TokenBucketRateLimiter requires `budget` or both `rate_per_sec` and `burst`"
                )
            budget = EndpointBudget(tokens_per_second=rate_per_sec, burst_capacity=burst)

        selected_clock = time_source or clock

        budget.validate(label="budget")
        for group, configured_budget in (group_budgets or {}).items():
            configured_budget.validate(label=f"group_budget[{group}]")

        self._clock = selected_clock
        self._sleep = sleep_fn
        self._lock = Lock()
        self._group_budgets = {"default": budget, **(group_budgets or {})}
        self._state = {
            group: {
                "tokens": float(configured_budget.burst_capacity),
                "updated_at": self._clock(),
                "cooldown_until": 0.0,
            }
            for group, configured_budget in self._group_budgets.items()
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

    def _wait_seconds_locked(self, group: str, cost: int) -> float:
        self._refill(group)
        state = self._state_for(group)
        budget = self._budget_for(group)
        now = self._clock()
        cooldown_wait = max(0.0, state["cooldown_until"] - now)
        if cooldown_wait > 0:
            return cooldown_wait
        if state["tokens"] >= cost:
            return 0.0
        deficit = float(cost) - state["tokens"]
        return deficit / max(budget.tokens_per_second, 1e-9)

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

    def _wait_seconds_locked(self, group: str, cost: int) -> float:
        self._refill(group)
        state = self._state_for(group)
        budget = self._budget_for(group)
        now = self._clock()
        cooldown_wait = max(0.0, state["cooldown_until"] - now)
        if cooldown_wait > 0:
            return cooldown_wait
        if state["tokens"] >= cost:
            return 0.0
        deficit = float(cost) - state["tokens"]
        return deficit / max(budget.tokens_per_second, 1e-9)

    def acquire(self, group: str, cost: int = 1) -> float:
        if cost < 1:
            return 0.0

        waited = 0.0
        while True:
            with self._lock:
                wait_seconds = self._wait_seconds_locked(group, cost)
                if wait_seconds == 0.0:
                    self._state_for(group)["tokens"] -= cost
                    return waited
            self._sleep(wait_seconds)
            waited += wait_seconds

    def consume(self, group: str = "default", cost: int = 1) -> bool:
        if cost < 1:
            return True
        with self._lock:
            wait_seconds = self._wait_seconds_locked(group, cost)
            if wait_seconds > 0:
                return False
            self._state_for(group)["tokens"] -= cost
            return True

    def seconds_until_available(self, group: str = "default", cost: int = 1) -> float:
        if cost < 1:
            return 0.0
        with self._lock:
            return max(0.0, self._wait_seconds_locked(group, cost))

    def penalize_on_429(self, group: str, retry_after_s: float | None) -> None:
        with self._lock:
            state = self._state_for(group)
            state["tokens"] = 0.0
            budget = self._budget_for(group)
            if retry_after_s is not None and retry_after_s > 0:
                state["cooldown_until"] = max(state["cooldown_until"], self._clock() + retry_after_s)
                return

            fallback_cooldown = min(1.5, max(0.25, 1.0 / budget.tokens_per_second))
            state["cooldown_until"] = max(state["cooldown_until"], self._clock() + fallback_cooldown)


class AsyncTokenBucketRateLimiter:
    def __init__(
        self,
        budget: EndpointBudget,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._sync = TokenBucketRateLimiter(
            budget,
            clock=clock,
            sleep_fn=lambda _: None,
        )
        self._sleep = sleep_fn

    async def acquire(self, group: str = "default", cost: int = 1) -> float:
        if cost < 1:
            return 0.0
        waited = 0.0
        while True:
            if self._sync.consume(group, cost):
                return waited
            wait_seconds = self._sync.seconds_until_available(group, cost)
            waited += wait_seconds
            await self._sleep(wait_seconds)


class AsyncTokenBucketRateLimiter:
    def __init__(
        self,
        *,
        rate_per_sec: float,
        burst: int,
        time_source: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sync = TokenBucketRateLimiter(
            rate_per_sec=rate_per_sec,
            burst=burst,
            time_source=time_source,
            sleep_fn=lambda _: None,
        )
        self._sleep = sleep_fn

    async def acquire(self, group: str = "default", cost: int = 1) -> float:
        if cost < 1:
            return 0.0
        waited = 0.0
        while True:
            if self._sync.consume(group, cost):
                return waited
            wait_seconds = self._sync.seconds_until_available(group, cost)
            waited += wait_seconds
            await self._sleep(wait_seconds)


def map_endpoint_group(path: str) -> str:
    normalized = path.lower()
    if "/orderbook" in normalized or "/ticker" in normalized or "/ohlc" in normalized:
        return "market_data"
    if "/order" in normalized and "/users/transactions" not in normalized:
        return "orders"
    if "/users/" in normalized or "/openorders" in normalized:
        return "account"
    return "market_data"
