from __future__ import annotations

from collections import OrderedDict
from time import perf_counter


class MetricsCollector:
    def __init__(self) -> None:
        self._starts: dict[str, float] = {}
        self._timings_ms: dict[str, int] = {}
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, object] = {}

    def start_timer(self, step_name: str) -> None:
        self._starts[step_name] = perf_counter()

    def stop_timer(self, step_name: str) -> int:
        start = self._starts.pop(step_name, None)
        if start is None:
            self._timings_ms[step_name] = 0
            return 0
        elapsed_ms = max(0, int(round((perf_counter() - start) * 1000)))
        self._timings_ms[step_name] = elapsed_ms
        return elapsed_ms

    def inc(self, name: str, value: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + value

    def set(self, name: str, value: object) -> None:
        self._gauges[name] = value

    def finalize(self) -> dict[str, object]:
        ordered: OrderedDict[str, object] = OrderedDict()
        for key in sorted(self._counters):
            ordered[key] = self._counters[key]
        for key in sorted(self._gauges):
            ordered[key] = self._gauges[key]

        total_ms = sum(self._timings_ms.values())
        ordered["latency_ms_total"] = total_ms
        for key in sorted(self._timings_ms):
            ordered[f"{key}_ms"] = self._timings_ms[key]
        return dict(ordered)
