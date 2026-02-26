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
    "bot_intents_created_total": MetricDef(
        name="bot_intents_created_total",
        type=MetricType.COUNTER,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_intents_executed_total": MetricDef(
        name="bot_intents_executed_total",
        type=MetricType.COUNTER,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_rejects_total": MetricDef(
        name="bot_rejects_total",
        type=MetricType.COUNTER,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_reject_1123_total": MetricDef(
        name="bot_reject_1123_total",
        type=MetricType.COUNTER,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_api_429_backoff_total": MetricDef(
        name="bot_api_429_backoff_total",
        type=MetricType.COUNTER,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_breaker_open": MetricDef(
        name="bot_breaker_open",
        type=MetricType.GAUGE,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_degraded_mode": MetricDef(
        name="bot_degraded_mode",
        type=MetricType.GAUGE,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_unknown_order_present": MetricDef(
        name="bot_unknown_order_present",
        type=MetricType.GAUGE,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_cursor_stall_total": MetricDef(
        name="bot_cursor_stall_total",
        type=MetricType.COUNTER,
        required_labels=("process_role", "mode_final"),
    ),
    "bot_rate_limit_wait_seconds": MetricDef(
        name="bot_rate_limit_wait_seconds",
        type=MetricType.HISTOGRAM,
        required_labels=("exchange", "endpoint", "process_role"),
    ),
    "bot_rate_limit_wait_total": MetricDef(
        name="bot_rate_limit_wait_total",
        type=MetricType.COUNTER,
        required_labels=("exchange", "endpoint", "process_role"),
    ),
    "bot_rest_retry_attempts_total": MetricDef(
        name="bot_rest_retry_attempts_total",
        type=MetricType.COUNTER,
        required_labels=("exchange", "endpoint", "error_kind", "process_role"),
    ),
    "bot_rest_retry_backoff_seconds": MetricDef(
        name="bot_rest_retry_backoff_seconds",
        type=MetricType.HISTOGRAM,
        required_labels=("exchange", "endpoint", "error_kind", "process_role"),
    ),

    "bot_idempotency_recovery_attempts_total": MetricDef(
        name="bot_idempotency_recovery_attempts_total",
        type=MetricType.COUNTER,
        required_labels=("exchange", "operation", "outcome", "process_role"),
    ),
    "bot_db_cycle_latency_ms": MetricDef(
        name="bot_db_cycle_latency_ms",
        type=MetricType.HISTOGRAM,
        required_labels=("process_role",),
    ),
    "bot_db_orders_submitted_total": MetricDef(
        name="bot_db_orders_submitted_total",
        type=MetricType.COUNTER,
        required_labels=("process_role",),
    ),
    "bot_db_rejects_total": MetricDef(
        name="bot_db_rejects_total",
        type=MetricType.COUNTER,
        required_labels=("process_role",),
    ),
    "bot_db_pnl_realized_quote": MetricDef(
        name="bot_db_pnl_realized_quote",
        type=MetricType.GAUGE,
        required_labels=("process_role",),
    ),
    "bot_db_pnl_unrealized_quote": MetricDef(
        name="bot_db_pnl_unrealized_quote",
        type=MetricType.GAUGE,
        required_labels=("process_role",),
    ),
}


DB_FIELD_METRIC_MAP: dict[str, str] = {
    "stage7_run_metrics.cycle_total_ms": "bot_db_cycle_latency_ms",
    "stage7_run_metrics.latency_ms_total": "bot_db_cycle_latency_ms",
    "cycle_metrics.orders_submitted": "bot_db_orders_submitted_total",
    "cycle_metrics.rejects_count": "bot_db_rejects_total",
    "stage7_ledger_metrics.realized_pnl_try": "bot_db_pnl_realized_quote",
    "stage7_ledger_metrics.unrealized_pnl_try": "bot_db_pnl_unrealized_quote",
}


def default_labels_for_db_field(field_name: str, *, process_role: str = "UNKNOWN") -> dict[str, str]:
    metric_name = DB_FIELD_METRIC_MAP[field_name]
    defn = REGISTRY[metric_name]
    labels: dict[str, str] = {}
    for label in defn.required_labels:
        if label == "process_role":
            labels[label] = process_role
        else:
            labels[label] = "unknown"
    return labels


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
