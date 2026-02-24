from __future__ import annotations

from btcbot.domain.risk_budget import Mode
from btcbot.obs.alert_engine import AlertDedupe, AlertRuleEvaluator, MetricWindowStore
from btcbot.obs.alerts import AlertRule
from btcbot.obs.stage4_alarm_hook import build_cycle_metrics


def test_condition_parser_rate_and_value() -> None:
    store = MetricWindowStore()
    evaluator = AlertRuleEvaluator()

    for i, ts in enumerate((0, 60, 120), start=1):
        store.record("bot_orders_failed_total", i * 20, ts)
    store.record("bot_breaker_open", 0, 0)
    store.record("bot_breaker_open", 1, 60)

    rate_rule = AlertRule(
        name="r1",
        metric_name="bot_orders_failed_total",
        condition="rate_per_minute > 10",
        severity="high",
        window="5m",
    )
    value_rule = AlertRule(
        name="r2",
        metric_name="bot_breaker_open",
        condition="value == 1",
        severity="high",
        window="5m",
    )

    fired = evaluator.evaluate_rules([rate_rule, value_rule], store, now_epoch=120)
    names = {event.rule_name for event in fired}
    assert names == {"r1", "r2"}

    not_fired = evaluator.evaluate_rules(
        [
            AlertRule(
                name="r3",
                metric_name="bot_breaker_open",
                condition="value == 2",
                severity="high",
                window="5m",
            )
        ],
        store,
        now_epoch=120,
    )
    assert not not_fired


def test_window_stats_delta_rate_and_p95() -> None:
    store = MetricWindowStore()
    store.record("x", 10, 0)
    store.record("x", 20, 60)
    store.record("x", 30, 120)
    store.record("x", 40, 180)

    stats = store.compute("5m", "x")

    assert stats["value"] == 40
    assert stats["delta"] == 30
    assert stats["rate_per_minute"] == 10
    assert stats["count"] == 4
    assert stats["p95"] == 40


def test_dedupe_respects_cooldown() -> None:
    dedupe = AlertDedupe(cooldown_by_severity={"high": 120})
    rule = AlertRule("r", "m", "value == 1", "high", "5m")
    evaluator = AlertRuleEvaluator()
    store = MetricWindowStore()

    store.record("m", 1, 0)
    events_1 = evaluator.evaluate_rules([rule], store, now_epoch=0)
    assert len(dedupe.filter(events_1)) == 1

    store.record("m", 1, 30)
    events_2 = evaluator.evaluate_rules([rule], store, now_epoch=30)
    assert len(dedupe.filter(events_2)) == 0


def test_stage4_hook_mapping_metrics() -> None:
    metrics = build_cycle_metrics(
        stage4_cycle_summary={
            "cycle_duration_ms": 250,
            "intents_created": 5,
            "intents_executed": 3,
            "orders_submitted": 2,
            "rejects_by_code": {"1123": 4, "5000": 1},
            "breaker_open": True,
        },
        reconcile_result={"api_429_backoff_total": 2},
        health_snapshot={"degraded": True},
        final_mode={"kill_switch": True, "observe_only": True},
        cursor_diag={"cursor_stall_by_symbol": {"BTC_TRY": 2, "ETH_TRY": 1}},
    )

    assert metrics["bot_cycle_latency_ms"] == 250
    assert metrics["bot_orders_failed_total"] == 5
    assert metrics["bot_reject_1123_total"] == 4
    assert metrics["bot_breaker_open"] == 1
    assert metrics["bot_degraded_mode"] == 1
    assert metrics["bot_cursor_stall_total"] == 3
    assert metrics["bot_killswitch_enabled"] == 1


def test_integration_smoke_breaker_rule_once_then_cooldown() -> None:
    store = MetricWindowStore()
    evaluator = AlertRuleEvaluator()
    dedupe = AlertDedupe(cooldown_by_severity={"high": 120})
    rule = AlertRule(
        name="breaker_open_persistent",
        metric_name="bot_breaker_open",
        condition="value == 1",
        severity="high",
        window="5m",
    )

    fired = 0
    for cycle in range(6):
        ts = cycle * 30
        store.record("bot_breaker_open", 1, ts)
        events = evaluator.evaluate_rules([rule], store, now_epoch=ts)
        fired += len(dedupe.filter(events))

    assert fired == 2


def test_observe_only_mapping_uses_mode_observe_only() -> None:
    metrics_monitor = build_cycle_metrics(
        stage4_cycle_summary={},
        reconcile_result=None,
        health_snapshot={"degraded": False},
        final_mode={"observe_only": Mode.NORMAL == Mode.OBSERVE_ONLY, "kill_switch": False},
        cursor_diag=None,
    )
    metrics_observe_only = build_cycle_metrics(
        stage4_cycle_summary={},
        reconcile_result=None,
        health_snapshot={"degraded": False},
        final_mode={"observe_only": Mode.OBSERVE_ONLY == Mode.OBSERVE_ONLY, "kill_switch": False},
        cursor_diag=None,
    )

    assert metrics_monitor["bot_degraded_mode"] == 0
    assert metrics_observe_only["bot_degraded_mode"] == 1
