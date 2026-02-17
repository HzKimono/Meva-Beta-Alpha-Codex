from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from btcbot.observability import get_instrumentation


def emit_decision(logger, envelope: Mapping[str, Any]) -> None:
    payload = dict(envelope)
    logger.info(
        "decision_event",
        extra={
            "cycle_id": payload.get("cycle_id"),
            "decision_layer": payload.get("decision_layer"),
            "reason_code": payload.get("reason_code"),
            "action": payload.get("action"),
            "extra": payload,
        },
    )
    try:
        get_instrumentation().counter(
            "decision_events_total",
            attrs={
                "decision_layer": str(payload.get("decision_layer", "unknown")),
                "reason_code": str(payload.get("reason_code", "unknown")),
                "action": str(payload.get("action", "unknown")),
            },
        )
    except Exception:  # noqa: BLE001
        return None
