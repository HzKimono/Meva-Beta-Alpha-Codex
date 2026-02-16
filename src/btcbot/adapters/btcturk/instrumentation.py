from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from btcbot.observability import get_instrumentation


class MetricsSink:
    def inc(self, name: str, value: int = 1, *, attrs: dict[str, Any] | None = None) -> None:  # noqa: D401
        """Increment a metric counter."""

    def observe_ms(
        self,
        name: str,
        value_ms: float,
        *,
        attrs: dict[str, Any] | None = None,
    ) -> None:  # noqa: D401
        """Observe latency in milliseconds."""

    def gauge(self, name: str, value: float, *, attrs: dict[str, Any] | None = None) -> None:
        """Record a gauge-like value."""


@dataclass
class InMemoryMetricsSink(MetricsSink):
    counters: Counter[str] = field(default_factory=Counter)
    latencies: dict[str, list[float]] = field(default_factory=dict)

    def inc(self, name: str, value: int = 1, *, attrs: dict[str, Any] | None = None) -> None:
        self.counters[name] += value
        get_instrumentation().counter(name, value, attrs=attrs)

    def observe_ms(
        self,
        name: str,
        value_ms: float,
        *,
        attrs: dict[str, Any] | None = None,
    ) -> None:
        self.latencies.setdefault(name, []).append(value_ms)
        get_instrumentation().histogram(name, value_ms, attrs=attrs)

    def gauge(self, name: str, value: float, *, attrs: dict[str, Any] | None = None) -> None:
        get_instrumentation().gauge(name, value, attrs=attrs)
