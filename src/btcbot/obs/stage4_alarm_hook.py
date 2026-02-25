from __future__ import annotations

from collections.abc import Mapping


def build_cycle_metrics(
    stage4_cycle_summary: dict,
    reconcile_result: dict | None,
    health_snapshot: dict | None,
    final_mode: dict,
    cursor_diag: dict | None,
) -> dict[str, float | int]:
    summary = stage4_cycle_summary or {}
    reconcile = reconcile_result or {}
    health = health_snapshot or {}
    mode = final_mode or {}
    cursor = cursor_diag or {}

    rejects_by_code = _coerce_dict(summary.get("rejects_by_code"))
    reject_total = sum(_to_int(value) for value in rejects_by_code.values())

    derived_orders_failed = _to_int(summary.get("orders_failed", reject_total))

    cursor_stall_by_symbol = _coerce_dict(cursor.get("cursor_stall_by_symbol"))
    if not cursor_stall_by_symbol:
        cursor_stall_by_symbol = _coerce_dict(reconcile.get("cursor_stall_by_symbol"))

    api_backoff_total = _to_int(
        health.get("api_429_backoff_total", reconcile.get("api_429_backoff_total", 0))
    )

    degraded_mode = bool(health.get("degraded", False) or mode.get("observe_only", False))
    breaker_open = bool(health.get("breaker_open", False) or summary.get("breaker_open", False))

    return {
        "bot_cycle_latency_ms": _to_int(summary.get("cycle_duration_ms", 0)),
        "bot_intents_created_total": _to_int(summary.get("intents_created", 0)),
        "bot_intents_executed_total": _to_int(summary.get("intents_executed", 0)),
        "bot_orders_submitted_total": _to_int(summary.get("orders_submitted", 0)),
        "bot_orders_failed_total": derived_orders_failed,
        "bot_rejects_total": reject_total,
        "bot_reject_1123_total": _to_int(rejects_by_code.get("1123", 0)),
        "bot_api_429_backoff_total": api_backoff_total,
        "bot_breaker_open": 1 if breaker_open else 0,
        "bot_degraded_mode": 1 if degraded_mode else 0,
        "bot_unknown_order_present": _to_int(
            summary.get("unknown_order_present", health.get("unknown_order_present", 0))
        ),
        "bot_cursor_stall_total": sum(_to_int(value) for value in cursor_stall_by_symbol.values()),
        "bot_killswitch_enabled": 1 if bool(mode.get("kill_switch", False)) else 0,
        "dryrun_market_data_stale_total": _to_int(summary.get("dryrun_market_data_stale_total", 0)),
        "dryrun_market_data_missing_symbols_total": _to_int(
            summary.get("dryrun_market_data_missing_symbols_total", 0)
        ),
        "dryrun_market_data_age_ms": _to_int(summary.get("dryrun_market_data_age_ms", 0)),
        "dryrun_ws_rest_fallback_total": _to_int(summary.get("dryrun_ws_rest_fallback_total", 0)),
        "dryrun_exchange_degraded_total": _to_int(summary.get("dryrun_exchange_degraded_total", 0)),
        "dryrun_submission_suppressed_total": _to_int(
            summary.get("dryrun_submission_suppressed_total", 0)
        ),
        "dryrun_cycle_duration_ms": _to_int(summary.get("cycle_duration_ms", 0)),
    }


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    return {}


def _to_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
