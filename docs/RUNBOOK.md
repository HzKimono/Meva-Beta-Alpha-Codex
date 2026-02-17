# btcbot Runbook

## Safety-first startup
- `SAFE_MODE=true` **always wins** and forces observe-only behavior.
- Keep `DRY_RUN=true` + `KILL_SWITCH=true` for first boot in any new environment.
- Only arm live mode after dry-run stability checks and explicit acknowledgement.

### Live mode toggle procedure
1. Confirm bot health and reconciliation stability for at least 30 minutes in dry-run.
2. Ensure no incident is active (429 storm, reconnect storm, stale data storm).
3. Set:
   - `SAFE_MODE=false`
   - `DRY_RUN=false`
   - `KILL_SWITCH=false`
   - `LIVE_TRADING=true`
   - `LIVE_TRADING_ACK=I_UNDERSTAND`
4. Restart service and verify startup banner does **not** show SAFE MODE.

### Emergency disable (immediate)
- Set `SAFE_MODE=true` and restart.
- This prevents submit/cancel write calls regardless of other flags.

## Startup recovery and crash/restart handling
On every `run` startup, recovery executes before trading side effects:
1. Reconcile open-order lifecycle state.
2. Refresh recent fills into accounting state.
3. Validate invariants (no negative balances / no negative position quantities).
4. If invariants fail, cycle is forced into observe-only behavior.

## Shutdown procedure
- Send normal termination signal.
- Service flushes observability pipelines and log handlers.
- HTTP/WS clients are closed best-effort before exit.

## Observability configuration
See [SLO targets](./SLO.md) for thresholds and paging conditions.

Vendor-neutral instrumentation is enabled by env flags:
- `OBSERVABILITY_ENABLED` (default `false`)
- `OBSERVABILITY_METRICS_EXPORTER` (`none|otlp|prometheus`)
- `OBSERVABILITY_OTLP_ENDPOINT` (when using OTLP)
- `OBSERVABILITY_PROMETHEUS_PORT` (default `9464`)

### Health signals (alertable)
- `ws_reconnect_rate`
- `rest_429_rate`
- `rest_retry_rate`
- `stale_market_data_rate`
- `reconcile_lag_ms`
- `order_submit_latency_ms`
- `cancel_latency_ms`
- `circuit_breaker_state`

## Incident playbooks
### 429 storm
- Symptoms: rising `rest_429_rate` + high `rest_retry_rate`.
- Actions:
  1. Set `SAFE_MODE=true`.
  2. Reduce request pressure (`BTCTURK_RATE_LIMIT_RPS`, burst).
  3. Increase retry delays (`BTCTURK_REST_BASE_DELAY_MS`, `BTCTURK_REST_MAX_DELAY_MS`).
  4. Re-enable gradually in dry-run.

### WS reconnect storm
- Symptoms: sustained `ws_reconnect_rate` elevation.
- Actions:
  1. Set `SAFE_MODE=true`.
  2. Validate network/DNS/TLS path and exchange status.
  3. Increase idle reconnect threshold and backoff if needed.
  4. Resume with dry-run only.

### Stale market data
- Symptoms: `stale_market_data_rate` > threshold.
- Actions:
  1. Stay in observe-only (`SAFE_MODE=true` or `KILL_SWITCH=true`).
  2. Check upstream market data feeds and clock skew.
  3. Resume live only when freshness is restored.

## Secret lifecycle controls
- Secrets are loaded through centralized startup providers (environment first, then optional dotenv source).
- Startup validates API scope least-privilege and secret rotation age.
- Required settings:
  - `BTCTURK_API_SCOPES` must contain `read` (and `trade` when live); must not contain `withdraw`.
  - `BTCTURK_SECRET_ROTATED_AT` should be ISO-8601.
  - `BTCTURK_SECRET_MAX_AGE_DAYS` controls max allowed age (default: `90`).

## API key rotation
1. Enable `SAFE_MODE=true`.
2. Replace `BTCTURK_API_KEY` and `BTCTURK_API_SECRET` securely.
3. Restart bot and run `doctor`/`health` checks.
4. Disable safe mode only after successful dry-run verification.

## Rollback checklist
1. Enable `SAFE_MODE=true`.
2. Roll back image/tag to previous known-good release.
3. Restart and verify startup recovery completes cleanly.
4. Confirm signal rates normalize before re-arming.

## Reproducible deployment
### Docker image build
```bash
docker build -t btcbot:local .
```

### Local compose run
```bash
docker compose up --build
```
- Uses `.env.live` and persistent volume for SQLite (`/data`).

### CI baseline checks
- lint + compile
- pytest
- mypy
- docker build
