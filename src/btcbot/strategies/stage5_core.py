from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from btcbot.domain.strategy_core import Intent, StrategyContext


class BaseStrategy(Protocol):
    id: str

    def generate_intents(self, context: StrategyContext) -> list[Intent]: ...


@dataclass(frozen=True)
class _RegistryEntry:
    strategy: BaseStrategy
    enabled: bool = True
    weight: int = 100


class StrategyRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}

    def register(self, strategy: BaseStrategy, *, enabled: bool = True, weight: int = 100) -> None:
        self._validate_weight(weight)
        if strategy.id in self._entries:
            raise ValueError(f"strategy id already registered: {strategy.id}")
        self._entries[strategy.id] = _RegistryEntry(
            strategy=strategy,
            enabled=enabled,
            weight=weight,
        )

    def replace(self, strategy: BaseStrategy, *, enabled: bool = True, weight: int = 100) -> None:
        self._validate_weight(weight)
        self._entries[strategy.id] = _RegistryEntry(
            strategy=strategy,
            enabled=enabled,
            weight=weight,
        )

    def set_enabled(self, strategy_id: str, enabled: bool) -> None:
        if strategy_id not in self._entries:
            raise KeyError(f"strategy id not found: {strategy_id}")
        entry = self._entries[strategy_id]
        self._entries[strategy_id] = _RegistryEntry(
            strategy=entry.strategy,
            enabled=enabled,
            weight=entry.weight,
        )

    def generate_intents(self, context: StrategyContext) -> list[Intent]:
        intents: list[Intent] = []
        for entry in sorted(
            self._entries.values(),
            key=lambda item: (-item.weight, item.strategy.id),
        ):
            if not entry.enabled:
                continue
            intents.extend(entry.strategy.generate_intents(context))
        return intents

    @staticmethod
    def _validate_weight(weight: int) -> None:
        if weight < 0:
            raise ValueError("weight must be >= 0")
