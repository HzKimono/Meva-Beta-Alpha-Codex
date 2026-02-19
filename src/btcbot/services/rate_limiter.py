from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic, sleep


@dataclass(frozen=True)
class EndpointBudget:
    name: str
    rps: float
    burst: int

    def validate(self) -> None:
        if self.rps <= 0:
            raise ValueError(f"EndpointBudget[{self.name}] rps must be > 0")
        if self.burst < 1:
            raise ValueError(f"EndpointBudget[{self.name}] burst must be >= 1")


class TokenBucketRateLimiter:
    def __init__(
        self,
        budgets: dict[str, EndpointBudget],
        *,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
    ) -> None:
        if not budgets:
            raise ValueError("budgets must include at least one group")
        if "default" not in budgets:
            raise ValueError("budgets must include 'default'")

        for budget in budgets.values():
            budget.validate()

        self._budgets = dict(budgets)
        self._clock = clock
        self._sleep = sleep_fn
        self._lock = Lock()
        self._state = {
            group: {
                "tokens": float(budget.burst),
                "updated_at": self._clock(),
                "cooldown_until": 0.0,
            }
            for group, budget in self._budgets.items()
        }

    def _budget_for(self, group: str) -> EndpointBudget:
        return self._budgets.get(group, self._budgets["default"])

    def _state_for(self, group: str) -> dict[str, float]:
        state = self._state.get(group)
        if state is None:
            budget = self._budget_for(group)
            state = {
                "tokens": float(budget.burst),
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
            state["tokens"] = min(float(budget.burst), state["tokens"] + elapsed * budget.rps)
            state["updated_at"] = now

    def _wait_seconds_locked(self, group: str, tokens: float) -> float:
        self._refill(group)
        state = self._state_for(group)
        budget = self._budget_for(group)
        now = self._clock()
        cooldown_wait = max(0.0, state["cooldown_until"] - now)
        if cooldown_wait > 0:
            return cooldown_wait
        if state["tokens"] >= tokens:
            return 0.0
        deficit = tokens - state["tokens"]
        return deficit / max(budget.rps, 1e-9)

    def acquire(self, group: str) -> float:
        waited = 0.0
        while True:
            with self._lock:
                wait_seconds = self._wait_seconds_locked(group, 1.0)
                if wait_seconds == 0.0:
                    self._state_for(group)["tokens"] -= 1.0
                    return waited
            self._sleep(wait_seconds)
            waited += wait_seconds

    def consume(self, group: str, tokens: float = 1.0) -> bool:
        if tokens <= 0:
            return True
        with self._lock:
            wait_seconds = self._wait_seconds_locked(group, tokens)
            if wait_seconds > 0:
                return False
            self._state_for(group)["tokens"] -= tokens
            return True

    def seconds_until_available(self, group: str, tokens: float = 1.0) -> float:
        if tokens <= 0:
            return 0.0
        with self._lock:
            return max(0.0, self._wait_seconds_locked(group, tokens))

    def penalize_on_429(self, group: str, retry_after_seconds: float | None = None) -> None:
        with self._lock:
            state = self._state_for(group)
            state["tokens"] = 0.0
            budget = self._budget_for(group)
            if retry_after_seconds is not None and retry_after_seconds > 0:
                state["cooldown_until"] = max(
                    state["cooldown_until"], self._clock() + retry_after_seconds
                )
                return

            fallback_cooldown = min(1.5, max(0.25, 1.0 / budget.rps))
            state["cooldown_until"] = max(state["cooldown_until"], self._clock() + fallback_cooldown)


class AsyncTokenBucketRateLimiter:
    def __init__(
        self,
        sync_limiter: TokenBucketRateLimiter,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sync_limiter = sync_limiter
        self._sleep = sleep_fn

    async def acquire(self, group: str) -> float:
        waited = 0.0
        while True:
            if self._sync_limiter.consume(group, 1.0):
                return waited
            wait_seconds = self._sync_limiter.seconds_until_available(group, 1.0)
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
