# P1.3-A Dry-run protocol (no trading)

## Required environment flags

```bash
export LIVE_TRADING=false
export SAFE_MODE=true
export KILL_SWITCH=true
```

## DB isolation (mandatory)

Use a unique `STATE_DB_PATH` per process. Do not share the same sqlite file between the trader loop and monitor/health process.

## Trader dry-run loop

```bash
export STATE_DB_PATH=./state/stage4-dryrun-trader.sqlite
python -m btcbot stage4-run --dry-run --loop --cycle-seconds 10 --max-cycles -1
```

On startup, verify the policy banner shows side effects blocked (`dry_run=True`, kill switch/safe mode active).

## Monitor / health loop

```bash
export STATE_DB_PATH=./state/stage4-dryrun-monitor.sqlite
while true; do
  python -m btcbot health
  sleep 30
done
```

## PASS / FAIL checklist (6-24h)

### PASS signals
- `dryrun_cycle_started_total` and `dryrun_cycle_completed_total` keep increasing.
- `dryrun_cycle_duration_ms` p95 remains under expected threshold.
- `dryrun_submission_suppressed_total` increases while no real order submissions happen.
- No exchange write side effects (no live submit/cancel calls).
- No repeated `ALERT` lines for high severity dry-run rules.

### FAIL signals
- `dryrun_cycle_completed_total` stalls.
- High stale ratio: `dryrun_market_data_stale_ratio_high` alert.
- Repeated degraded exchange snapshots: `dryrun_exchange_degraded_consecutive` alert.
- REST fallback spikes: `dryrun_ws_rest_fallback_spike` alert.
- Long cycle latency p95: `dryrun_cycle_duration_p95_high` alert.
- Stalled cycles: `dryrun_cycle_stalled` alert.

## Metrics to watch

- `dryrun_cycle_started_total`
- `dryrun_cycle_completed_total`
- `dryrun_cycle_duration_ms`
- `dryrun_market_data_stale_total`
- `dryrun_market_data_missing_symbols_total`
- `dryrun_market_data_age_ms`
- `dryrun_ws_rest_fallback_total`
- `dryrun_exchange_degraded_total`
- `dryrun_submission_suppressed_total`

## Local reliability check for test isolation

Run the full suite multiple times to verify DB isolation and lock stability:

```bash
for i in 1 2 3 4 5; do
  echo "run $i"
  pytest -q
done
```
