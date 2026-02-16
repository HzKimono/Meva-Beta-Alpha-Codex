from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


class MetricsSink:
    def inc(self, name: str, value: int = 1) -> None:  # noqa: D401
        """Increment a metric counter."""

    def observe_ms(self, name: str, value_ms: float) -> None:  # noqa: D401
        """Observe latency in milliseconds."""


@dataclass
class InMemoryMetricsSink(MetricsSink):
    counters: Counter[str] = field(default_factory=Counter)
    latencies: dict[str, list[float]] = field(default_factory=dict)

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] += value

    def observe_ms(self, name: str, value_ms: float) -> None:
        self.latencies.setdefault(name, []).append(value_ms)
