# Stage 6.2: Execution Quality + Atomicity

## Transactional cycle boundary

Stage 6.2 introduces a `StateStore.transaction()` context that starts a SQLite `BEGIN IMMEDIATE` transaction and commits/rolls back as one unit.

Within a cycle, the following now execute atomically:
- ledger event append
- accounting fill application
- fill cursor advancement (`fills_cursor:*`)
- cycle metrics persistence (`cycle_metrics`)

If any step fails, the transaction is rolled back and cursor values are unchanged. This guarantees replay-safe retries.

## Cursor safety and idempotency

`AccountingService.fetch_new_fills()` no longer advances cursors. It returns fetched fills plus `cursor_after` candidate.

Cursor advancement happens only inside the transactional block in the cycle runner, after ledger/accounting updates succeed.

Idempotent fill application is enforced through `applied_fills(fill_id PRIMARY KEY)`. Reprocessing the same fill is ignored in accounting state mutation.

## Execution quality metrics schema

`cycle_metrics` table captures one row per cycle (`cycle_id` PK), including:
- timestamps: `ts_start`, `ts_end`
- mode (`NORMAL`, `OBSERVE_ONLY`, etc.)
- counts: fills/submitted/canceled/rejects
- quality: `fill_rate`, `avg_time_to_fill`, `slippage_bps_avg`
- JSON payloads: `fees_json`, `pnl_json`, `meta_json`

Indexes exist on `ts_start` and `mode`.

Metrics are computed via pure domain logic (`domain/execution_quality.py`) and persisted through `services/metrics_service.py`.
