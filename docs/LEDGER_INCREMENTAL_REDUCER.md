# Ledger Incremental Reducer (Stage7)

## Schema
A persisted reducer checkpoint is stored in `ledger_reducer_checkpoints`:

- `scope_id TEXT PRIMARY KEY` (`"stage7"` for Stage7 reducer usage)
- `last_rowid INTEGER NOT NULL`
- `snapshot_json TEXT NOT NULL`
- `snapshot_version INTEGER NOT NULL`
- `updated_at TEXT NOT NULL`

`ledger_events` remains the immutable event log. Incremental fetches use:

```sql
SELECT rowid, event_id, ts, symbol, type, side, qty, price, fee, fee_currency,
       exchange_trade_id, exchange_order_id, client_order_id, meta_json
FROM ledger_events
WHERE rowid > ?
ORDER BY rowid ASC;
```

## Algorithm
`LedgerService.load_state_incremental(scope_id="stage7")`:

1. Read checkpoint.
2. If checkpoint exists and `snapshot_version` matches, deserialize `snapshot_json` and restore cursor from `last_rowid`.
3. Otherwise fallback to empty `LedgerState()` and cursor `0`.
4. Load only events after cursor rowid.
5. Apply batch with `apply_events` (unchanged deterministic ordering by `(ensure_utc(ts), event_id)`).
6. Advance checkpoint cursor to the **max rowid in the fetched/applied batch only**.
   - If no rows were fetched, cursor remains unchanged.
   - This prevents skipping events appended concurrently after the fetch.
7. Persist checkpoint only when state/cursor changes.

## Determinism
- Event reducer semantics are unchanged: `apply_events` still sorts by `(ensure_utc(ts), event_id)`.
- Snapshot serialization is deterministic:
  - Decimals serialized as strings.
  - Datetimes serialized as timezone-aware ISO-8601 strings.
  - Symbol keys and fee currency keys are sorted.
  - Lots preserve stored order.
- Deserialization normalizes lot timestamps to UTC (`ensure_utc`).

## Ordering assumption / late timestamp limitation
Incremental loading is rowid-based and applies only rows appended after `last_rowid`.

For strict equivalence with a single global replay sorted by `(ensure_utc(ts), event_id)`, ingestion should append events in non-decreasing timestamp order. If older timestamps are appended later (higher rowid but older `ts`), the reducer remains deterministic per batch and across runs, but may differ from a hypothetical full global re-sort over the entire table.

## Failure modes / fallback
- Missing checkpoint: full rebuild once from rowid `0`.
- Corrupt checkpoint JSON: ignored, full rebuild once, then checkpoint overwritten.
- Version mismatch: ignored, full rebuild once, then checkpoint overwritten with current version.
- No-new-events with existing checkpoint: checkpoint is reused without writing a new row version (no churn).

## Stage7 usage
Stage7 calls `ledger_service.load_state_incremental(scope_id="stage7")` once per cycle and reuses the returned `LedgerState` for position updates and snapshot computations. This removes repeated full ledger replay in normal operation.
