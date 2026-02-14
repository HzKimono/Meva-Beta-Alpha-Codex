from btcbot.services.metrics_collector import MetricsCollector


def test_metrics_collector_counters_and_gauges_deterministic() -> None:
    collector = MetricsCollector()
    collector.inc("a")
    collector.inc("a", 2)
    collector.set("z", 9)
    collector.set("m", 3)

    payload = collector.finalize()

    assert payload["a"] == 3
    assert payload["m"] == 3
    assert payload["z"] == 9
    assert payload["latency_ms_total"] >= 0


def test_metrics_collector_timer_non_negative() -> None:
    collector = MetricsCollector()
    collector.start_timer("selection")
    elapsed = collector.stop_timer("selection")
    payload = collector.finalize()

    assert elapsed >= 0
    assert payload["selection_ms"] >= 0
    assert payload["latency_ms_total"] >= payload["selection_ms"]
