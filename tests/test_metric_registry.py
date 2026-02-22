from __future__ import annotations

import pytest

from btcbot.obs.metric_registry import REGISTRY, MetricDef, MetricType, validate_registry
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
