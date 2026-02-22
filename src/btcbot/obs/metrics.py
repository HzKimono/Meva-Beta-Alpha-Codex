from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Protocol

from btcbot.obs.metric_registry import MetricDef, MetricType, REGISTRY

logger = logging.getLogger(__name__)


class MetricsSink(Protocol):
    def emit(self, defn: MetricDef, value: float | int | Decimal, labels: dict[str, str]) -> None:
        ...


class LoggingMetricsSink:
    def emit(self, defn: MetricDef, value: float | int | Decimal, labels: dict[str, str]) -> None:
        logger.info(
            "metric_emit",
            extra={
                "extra": {
                    "metric_name": defn.name,
                    "metric_type": defn.type.value,
                    "metric_value": str(value),
                    "labels": labels,
                }
            },
        )


_DEFAULT_SINK: MetricsSink = LoggingMetricsSink()
_STRICT_REGISTRY = os.getenv("OBS_METRICS_STRICT", "1") != "0"


def set_metrics_sink(sink: MetricsSink) -> None:
    global _DEFAULT_SINK
    _DEFAULT_SINK = sink


def _validate_labels(defn: MetricDef, labels: dict[str, str]) -> None:
    missing = [label for label in defn.required_labels if label not in labels]
    if missing:
        raise ValueError(f"missing labels for {defn.name}: {missing}")


def emit_metric(name: str, value: float | int | Decimal, labels: dict[str, str]) -> None:
    defn = REGISTRY.get(name)
    if defn is None:
        message = f"unknown metric name: {name}"
        if _STRICT_REGISTRY:
            raise ValueError(message)
        logger.error("metric_unknown", extra={"extra": {"name": name}})
        return
    _validate_labels(defn, labels)
    _DEFAULT_SINK.emit(defn, value, labels)


def inc_counter(name: str, labels: dict[str, str], delta: int = 1) -> None:
    defn = REGISTRY.get(name)
    if defn is None:
        emit_metric(name, delta, labels)
        return
    if defn.type is not MetricType.COUNTER:
        raise ValueError(f"metric {name} is not a counter")
    emit_metric(name, delta, labels)


def set_gauge(name: str, value: float | int | Decimal, labels: dict[str, str]) -> None:
    defn = REGISTRY.get(name)
    if defn is None:
        emit_metric(name, value, labels)
        return
    if defn.type is not MetricType.GAUGE:
        raise ValueError(f"metric {name} is not a gauge")
    emit_metric(name, value, labels)


def observe_histogram(name: str, value: float | int | Decimal, labels: dict[str, str]) -> None:
    defn = REGISTRY.get(name)
    if defn is None:
        emit_metric(name, value, labels)
        return
    if defn.type is not MetricType.HISTOGRAM:
        raise ValueError(f"metric {name} is not a histogram")
    emit_metric(name, value, labels)
