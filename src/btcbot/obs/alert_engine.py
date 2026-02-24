from __future__ import annotations

import json
import logging
import math
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from btcbot.obs.alerts import AlertRule

logger = logging.getLogger(__name__)

_ALLOWED_IDENTIFIERS = {"value", "delta", "rate_per_minute", "p95", "count"}
_CONDITION_RE = re.compile(
    r"^\s*(?P<left>value|delta|rate_per_minute|p95|count)\s*"
    r"(?P<op>==|!=|>=|<=|>|<)\s*(?P<right>-?\d+(?:\.\d+)?)\s*$"
)


@dataclass(frozen=True)
class AlertEvent:
    ts_epoch: int
    rule_name: str
    severity: str
    metric_name: str
    window: str
    value: float
    details: dict[str, object] = field(default_factory=dict)


class MetricWindowStore:
    def __init__(self, max_samples_per_metric: int = 2048) -> None:
        self._max_samples_per_metric = max(2, max_samples_per_metric)
        self._series: dict[str, deque[tuple[int, float]]] = {}

    def record(self, metric_name: str, value: float | int, ts_epoch: int) -> None:
        series = self._series.setdefault(
            metric_name, deque(maxlen=self._max_samples_per_metric)
        )
        series.append((int(ts_epoch), float(value)))

    def compute(self, window: str, metric_name: str) -> dict[str, float]:
        series = self._series.get(metric_name)
        if not series:
            return {"value": 0.0, "delta": 0.0, "rate_per_minute": 0.0, "p95": 0.0, "count": 0.0}

        window_seconds = _parse_window_to_seconds(window)
        latest_ts = series[-1][0]
        cutoff = latest_ts - window_seconds
        samples = [(ts, value) for ts, value in series if ts >= cutoff]
        if not samples:
            return {"value": 0.0, "delta": 0.0, "rate_per_minute": 0.0, "p95": 0.0, "count": 0.0}

        first_ts, first_value = samples[0]
        last_ts, last_value = samples[-1]
        delta = last_value - first_value
        elapsed_minutes = max((last_ts - first_ts) / 60.0, 0.0)
        rate_per_minute = (delta / elapsed_minutes) if elapsed_minutes > 0 else 0.0

        values = [item[1] for item in samples]
        p95 = _percentile(values, 95)

        return {
            "value": float(last_value),
            "delta": float(delta),
            "rate_per_minute": float(rate_per_minute),
            "p95": float(p95),
            "count": float(len(samples)),
        }


class AlertRuleEvaluator:
    def parse_condition(self, condition: str) -> tuple[str, str, float]:
        match = _CONDITION_RE.match(condition)
        if match is None:
            raise ValueError(f"unsupported_alert_condition:{condition}")
        left = match.group("left")
        if left not in _ALLOWED_IDENTIFIERS:
            raise ValueError(f"unsupported_alert_identifier:{left}")
        return left, match.group("op"), float(match.group("right"))

    def evaluate_rules(
        self,
        rules: list[AlertRule],
        store: MetricWindowStore,
        now_epoch: int,
    ) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for rule in rules:
            stats = store.compute(rule.window, rule.metric_name)
            try:
                left, op, right = self.parse_condition(rule.condition)
            except ValueError:
                logger.warning(
                    "alert_rule_parse_failed",
                    extra={"extra": {"rule": rule.name, "condition": rule.condition}},
                )
                continue
            left_value = float(stats.get(left, 0.0))
            if _compare(left_value, op, right):
                events.append(
                    AlertEvent(
                        ts_epoch=int(now_epoch),
                        rule_name=rule.name,
                        severity=rule.severity,
                        metric_name=rule.metric_name,
                        window=rule.window,
                        value=left_value,
                        details={
                            "condition": rule.condition,
                            "stats": stats,
                        },
                    )
                )
        return events


class AlertDedupe:
    _SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

    def __init__(
        self,
        cooldown_by_severity: Mapping[str, int] | None = None,
        significant_change_ratio: float = 0.10,
    ) -> None:
        self._cooldown_by_severity = {
            "critical": 60,
            "high": 120,
            "medium": 300,
            "low": 600,
        }
        if cooldown_by_severity:
            self._cooldown_by_severity.update(
                {str(k): int(v) for k, v in cooldown_by_severity.items()}
            )
        self._significant_change_ratio = max(0.0, significant_change_ratio)
        self._last_fired_by_rule: dict[str, AlertEvent] = {}

    def filter(self, events: Iterable[AlertEvent]) -> list[AlertEvent]:
        emitted: list[AlertEvent] = []
        for event in events:
            if self._should_emit(event):
                self._last_fired_by_rule[event.rule_name] = event
                emitted.append(event)
        return emitted

    def _should_emit(self, event: AlertEvent) -> bool:
        previous = self._last_fired_by_rule.get(event.rule_name)
        if previous is None:
            return True

        prev_rank = self._SEVERITY_RANK.get(previous.severity, 0)
        next_rank = self._SEVERITY_RANK.get(event.severity, 0)
        if next_rank > prev_rank:
            return True

        cooldown = self._cooldown_by_severity.get(event.severity, 300)
        if event.ts_epoch - previous.ts_epoch >= cooldown:
            return True

        prev_value = previous.value
        if prev_value == 0:
            return abs(event.value - prev_value) > 0
        pct_change = abs(event.value - prev_value) / abs(prev_value)
        return pct_change > self._significant_change_ratio


class Notifier:
    def notify(self, event: AlertEvent) -> None:  # pragma: no cover - protocol-style interface
        raise NotImplementedError


class LogNotifier(Notifier):
    def __init__(self, source_logger: logging.Logger | None = None) -> None:
        self._logger = source_logger or logger

    def notify(self, event: AlertEvent) -> None:
        payload = {
            "ts_epoch": event.ts_epoch,
            "rule_name": event.rule_name,
            "severity": event.severity,
            "metric_name": event.metric_name,
            "window": event.window,
            "value": event.value,
            "details": event.details,
        }
        self._logger.warning("ALERT %s", json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _parse_window_to_seconds(window: str) -> int:
    match = re.fullmatch(r"(?P<n>\d+)(?P<u>[smhd])", str(window).strip().lower())
    if match is None:
        raise ValueError(f"unsupported_window:{window}")
    n = int(match.group("n"))
    unit = match.group("u")
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * multiplier


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = max(0, min(len(sorted_values) - 1, math.ceil((percentile / 100) * len(sorted_values)) - 1))
    return float(sorted_values[index])


def _compare(left: float, op: str, right: float) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    raise ValueError(f"unsupported_operator:{op}")
