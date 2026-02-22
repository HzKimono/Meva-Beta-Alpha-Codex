from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class MetricType(Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True)
class MetricDef:
    name: str
    type: MetricType
    required_labels: tuple[str, ...] = ()


REGISTRY: dict[str, MetricDef] = {
    "bot_cycle_latency_ms": MetricDef(
        name="bot_cycle_latency_ms",
        type=MetricType.HISTOGRAM,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_api_errors_total": MetricDef(
        name="bot_api_errors_total",
        type=MetricType.COUNTER,
        required_labels=("exchange", "endpoint", "process_role"),
    ),
    "bot_ws_disconnects_total": MetricDef(
        name="bot_ws_disconnects_total",
        type=MetricType.COUNTER,
        required_labels=("exchange", "process_role"),
    ),
    "bot_orders_submitted_total": MetricDef(
        name="bot_orders_submitted_total",
        type=MetricType.COUNTER,
        required_labels=("symbol", "side", "process_role"),
    ),
    "bot_orders_failed_total": MetricDef(
        name="bot_orders_failed_total",
        type=MetricType.COUNTER,
        required_labels=("symbol", "reason", "process_role"),
    ),
    "bot_killswitch_enabled": MetricDef(
        name="bot_killswitch_enabled",
        type=MetricType.GAUGE,
        required_labels=("process_role",),
    ),
    "bot_exposure_base": MetricDef(
        name="bot_exposure_base",
        type=MetricType.GAUGE,
        required_labels=("symbol", "process_role"),
    ),
    "bot_pnl_realized_quote": MetricDef(
        name="bot_pnl_realized_quote",
        type=MetricType.GAUGE,
        required_labels=("quote", "process_role"),
    ),
    "bot_pnl_unrealized_quote": MetricDef(
        name="bot_pnl_unrealized_quote",
        type=MetricType.GAUGE,
        required_labels=("quote", "process_role"),
    ),
}


DB_FIELD_METRIC_MAP: dict[str, str] = {
    "stage7_run_metrics.cycle_total_ms": "bot_cycle_latency_ms",
    "stage7_run_metrics.latency_ms_total": "bot_cycle_latency_ms",
    "cycle_metrics.orders_submitted": "bot_orders_submitted_total",
    "cycle_metrics.rejects_count": "bot_orders_failed_total",
    "stage7_ledger_metrics.realized_pnl_try": "bot_pnl_realized_quote",
    "stage7_ledger_metrics.unrealized_pnl_try": "bot_pnl_unrealized_quote",
}


_NAME_PATTERN = re.compile(r"^[a-z]+(?:_[a-z0-9]+)+$")


def validate_registry(registry: dict[str, MetricDef] | None = None) -> None:
    target = registry or REGISTRY
    if len(target) != len(set(target.keys())):
        raise ValueError("metric registry contains duplicate keys")

    for key, metric in target.items():
        if key != metric.name:
            raise ValueError(f"registry key/name mismatch: {key} != {metric.name}")
        if not _NAME_PATTERN.match(metric.name):
            raise ValueError(f"invalid metric name format: {metric.name}")
        if not metric.name.startswith("bot_"):
            raise ValueError(f"metric name must use bot_ namespace: {metric.name}")
        if (
            metric.type in {MetricType.COUNTER, MetricType.GAUGE, MetricType.HISTOGRAM}
            and not metric.required_labels
        ):
            raise ValueError(f"required_labels must be non-empty for {metric.name}")


validate_registry()
