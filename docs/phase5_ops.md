# Phase 5 Ops Runbook (Observability + Self-healing)

## Key metrics
- `bot_api_errors_total{exchange,endpoint,process_role}`: **per-attempt** semantics (each HTTP attempt that returns an error increments once).
- `bot_api_429_backoff_total{process_role,mode_final}`: **per-attempt** on HTTP 429 only.
- `bot_rest_retry_attempts_total{exchange,endpoint,error_kind,process_role}`: retry classification attempts.
- `bot_rest_retry_backoff_seconds{exchange,endpoint,error_kind,process_role}`: selected retry delay.
- `bot_rate_limit_wait_seconds{exchange,endpoint,process_role}` / `bot_rate_limit_wait_total{...}`: local rate-limit waiting pressure.
- `bot_idempotency_recovery_attempts_total{exchange,operation,outcome,process_role}`: safe submit/cancel recovery outcomes (`found`, `not_found`, `error`).

Example: one logical request with 3 attempts where all attempts get `429` will increment both `bot_api_errors_total` and `bot_api_429_backoff_total` by `+3`.

## Alert tuning notes
- `api_error_rate_spike` is driven by **attempt-level** failures; bursts of retries can raise it quickly during exchange throttling.
- Use `bot_api_429_backoff_total` to distinguish exchange throttling from non-429 API failures.
- If `bot_api_errors_total` rises while `bot_api_429_backoff_total` stays flat, prioritize non-rate-limit incident triage.
- `mode_final=unknown` means the metric was emitted in a low-level adapter context where runtime mode was not available yet; treat it as transport-layer telemetry.

## Safe DB unlock procedure
1. Inspect lock state:
   - `python -m btcbot.cli state-db-locks list --db <STATE_DB_PATH>`
2. Attempt safe unlock (no force):
   - `python -m btcbot.cli state-db-unlock --db <STATE_DB_PATH> --instance-id <ID>`
3. Only if operator has confirmed instance is dead, force unlock with loud ack:
   - `python -m btcbot.cli state-db-unlock --db <STATE_DB_PATH> --instance-id <ID> --force --force-ack I_UNDERSTAND_STATE_DB_UNLOCK`

`--force` requires the exact acknowledgement string and emits `state_db_unlock_audit` in logs.

## Relevant environment/config inputs
- `STATE_DB_PATH`
- `PROCESS_ROLE`
- `LIVE_TRADING`, `LIVE_TRADING_ACK`, `KILL_SWITCH`, `SAFE_MODE`
- `OBS_METRICS_STRICT`
