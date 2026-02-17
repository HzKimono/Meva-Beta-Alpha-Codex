# Threat Model and Risk Register

## Scope
Live-trading runtime, WS/REST market-data ingestion, order submission path, secret lifecycle, and CI controls.

## Threat model summary
- **Assets**: API credentials, order intent stream, account balances/positions, replay fixtures, audit trail.
- **Trust boundaries**: local runtime env, BTCTurk API/WS endpoints, CI pipeline, local SQLite state.
- **Adversaries/failures**: credential leakage, over-privileged API keys, exchange throttling/outages, reconnect storms, time skew.

## Risk register
| ID | Risk | Likelihood | Impact | Controls |
|---|---|---:|---:|---|
| R-01 | API key leakage in logs/audit payloads | M | H | central redaction in logging and audit payload compaction |
| R-02 | Over-privileged API key (withdraw scope) | M | H | startup scope validation rejects `withdraw` |
| R-03 | Secret staleness / no rotation hygiene | M | M | startup rotation-age validation via `BTCTURK_SECRET_ROTATED_AT` + max age |
| R-04 | Unsafe runtime mode enabled accidentally | M | H | `SAFE_MODE=true` secure default, live-trading requires explicit override |
| R-05 | WS reconnect storms degrade decisions | H | M | reconnect metrics/SLOs + chaos tests |
| R-06 | REST 429 storm causes cascading latency | H | M | Retry-After honoring + retry chaos tests |
| R-07 | Partial outages/timeouts create stale plans | H | M | timeout retry, stale data alerts, observe-only fallback |
| R-08 | Clock skew breaks freshness/reconcile checks | M | M | anomaly detection + chaos test for skew events |
| R-09 | Reconcile lag drifts above safe bounds | M | H | SLO alerts + rollback/emergency runbook |

## File plan
- `src/btcbot/security/redaction.py`: reusable redaction primitives for logs/audit payloads.
- `src/btcbot/security/secrets.py`: provider abstraction, startup secret injection, scope/rotation validation.
- `src/btcbot/config.py`: secure defaults and live-trading safety constraints.
- `src/btcbot/cli.py`: centralized secret loading + startup validation enforcement.
- `tests/chaos/`: reconnect/429/timeout/clock-skew resilience tests.
- `tests/soak/`: WS ingest + 24h simulated fixture soak tests.
- `docs/SLO.md`, `docs/RUNBOOK.md`: SLOs, alerts, and operational procedures.
- `.github/workflows/ci.yml`: unit/integration/soak/security stages.

## Rollout plan
1. **Phase 0 (observe-only)**: keep `SAFE_MODE=true`, deploy and monitor SLO dashboards.
2. **Phase 1 (dry-run)**: set `SAFE_MODE=false`, keep `DRY_RUN=true`, validate 24h soak + chaos in CI/nightly.
3. **Phase 2 (armed live)**: `SAFE_MODE=false`, `DRY_RUN=false`, `LIVE_TRADING=true`, `LIVE_TRADING_ACK=I_UNDERSTAND`, and pass doctor checks.
4. **Phase 3 (steady-state)**: rotate keys every <= `BTCTURK_SECRET_MAX_AGE_DAYS`; enforce paging on SLO breach.

## Required env flags
- `SAFE_MODE` (default `true`)
- `BTCTURK_API_SCOPES` (must include `read`, include `trade` for live, must NOT include `withdraw`)
- `BTCTURK_SECRET_ROTATED_AT` (ISO-8601 timestamp)
- `BTCTURK_SECRET_MAX_AGE_DAYS` (default `90`)
- Existing arming flags: `DRY_RUN`, `KILL_SWITCH`, `LIVE_TRADING`, `LIVE_TRADING_ACK`
