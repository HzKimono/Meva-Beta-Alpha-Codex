from __future__ import annotations

import pytest

from btcbot.obs.metric_registry import (
    DB_FIELD_METRIC_MAP,
    REGISTRY,
    MetricDef,
    MetricType,
    default_labels_for_db_field,
    validate_registry,
)
from btcbot.obs.metrics import emit_metric


def test_validate_registry_passes() -> None:
    validate_registry(REGISTRY)


def test_validate_registry_fails_on_key_name_mismatch() -> None:
    bad = {
        "bot_ok_metric": MetricDef(
            name="bot_other_metric", type=MetricType.COUNTER, required_labels=("process_role",)
        )
    }
    with pytest.raises(ValueError):
        validate_registry(bad)


def test_unknown_metric_emission_fails() -> None:
    with pytest.raises(ValueError):
        emit_metric("bot_unknown_metric_total", 1, {"process_role": "LIVE"})


def test_phase5_metrics_registered_for_stage4_and_rest_instrumentation() -> None:
    expected = {
        "bot_intents_created_total",
        "bot_intents_executed_total",
        "bot_rejects_total",
        "bot_reject_1123_total",
        "bot_api_429_backoff_total",
        "bot_breaker_open",
        "bot_degraded_mode",
        "bot_unknown_order_present",
        "bot_cursor_stall_total",
        "bot_rate_limit_wait_seconds",
        "bot_rate_limit_wait_total",
        "bot_rest_retry_attempts_total",
        "bot_rest_retry_backoff_seconds",
        "bot_idempotency_recovery_attempts_total",
        "bot_db_cycle_latency_ms",
        "bot_db_orders_submitted_total",
        "bot_db_rejects_total",
        "bot_db_pnl_realized_quote",
        "bot_db_pnl_unrealized_quote",
    }
    assert expected.issubset(REGISTRY.keys())


@pytest.mark.parametrize("db_field,metric_name", sorted(DB_FIELD_METRIC_MAP.items()))
def test_db_field_metric_map_default_labels_satisfy_registry(
    db_field: str,
    metric_name: str,
) -> None:
    labels = default_labels_for_db_field(db_field, process_role="LIVE")
    required = REGISTRY[metric_name].required_labels
    assert all(label in labels and labels[label] for label in required)
