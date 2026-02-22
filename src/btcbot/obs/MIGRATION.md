# Observability Migration Notes

This change introduces canonical metric names under `btcbot.obs.metric_registry.REGISTRY` and wires core paths.

## Migrated now
- Stage 4 and Stage 7 cycle latency/kill-switch signals emit canonical metrics.
- BTCTurk REST errors and websocket disconnects emit canonical counters.
- Execution submit/failure counts emit canonical counters.

## Legacy metric emissions still present
The following legacy/non-canonical streams still exist and should be migrated incrementally:
- `btcbot.observability.get_instrumentation()` ad-hoc names in services and CLI.
- `btcbot.adapters.btcturk.instrumentation.MetricsSink` names (e.g. `rest_*`, `ws_*`).
- Stage7 DB persistence fields in `stage7_run_metrics` and `cycle_metrics` remain unchanged by design; map via `DB_FIELD_METRIC_MAP`.

## Suggested next steps
1. Move remaining `get_instrumentation().counter/gauge/histogram` calls to `btcbot.obs.metrics` helpers.
2. Add per-process role propagation for all CLI entrypoints and daemons.
3. Add adapter-level labels for symbol/endpoint normalization and alert dashboards.
