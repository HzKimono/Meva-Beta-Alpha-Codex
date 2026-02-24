from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertRule:
    name: str
    metric_name: str
    condition: str
    severity: str
    window: str


BASELINE_ALERT_RULES: list[AlertRule] = [
    AlertRule(
        name="api_error_rate_spike",
        metric_name="bot_api_errors_total",
        condition="rate_per_minute > 10",
        severity="high",
        window="5m",
    ),
    AlertRule(
        name="cycle_latency_high",
        metric_name="bot_cycle_latency_ms",
        condition="p95 > 2500",
        severity="medium",
        window="10m",
    ),
    AlertRule(
        name="ws_disconnect_burst",
        metric_name="bot_ws_disconnects_total",
        condition="delta > 5",
        severity="high",
        window="5m",
    ),
    AlertRule(
        name="killswitch_stuck_enabled",
        metric_name="bot_killswitch_enabled",
        condition="value == 1",
        severity="critical",
        window="10m",
    ),
    AlertRule(
        name="order_failures_spike",
        metric_name="bot_orders_failed_total",
        condition="rate_per_minute > 5",
        severity="high",
        window="5m",
    ),
    AlertRule(
        name="stuck_cycles",
        metric_name="bot_orders_submitted_total",
        condition="delta == 0",
        severity="high",
        window="10m",
    ),
    AlertRule(
        name="breaker_open_persistent",
        metric_name="bot_breaker_open",
        condition="value == 1",
        severity="high",
        window="5m",
    ),
    AlertRule(
        name="reject_spike_1123",
        metric_name="bot_reject_1123_total",
        condition="rate_per_minute > 3",
        severity="medium",
        window="10m",
    ),
    AlertRule(
        name="cursor_stall_spike",
        metric_name="bot_cursor_stall_total",
        condition="delta > 0",
        severity="medium",
        window="10m",
    ),
    AlertRule(
        name="degraded_mode_stuck",
        metric_name="bot_degraded_mode",
        condition="value == 1",
        severity="high",
        window="10m",
    ),
]


def format_alert_rules(rules: list[AlertRule] | None = None) -> str:
    target = rules or BASELINE_ALERT_RULES
    lines = ["name,metric_name,condition,severity,window"]
    for rule in target:
        lines.append(
            f"{rule.name},{rule.metric_name},{rule.condition},{rule.severity},{rule.window}"
        )
    return "\n".join(lines)
