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
2. If checkpoint exists and `snapshot_version` matches, deserialize `snapshot_json`.
3. Otherwise fallback to empty `LedgerState()` and cursor `0`.
4. Load only events after cursor rowid.
5. Apply batch with `apply_events` (unchanged deterministic ordering by `(ensure_utc(ts), event_id)`).
6. Persist checkpoint with latest rowid and serialized state snapshot.

## Determinism
- Event reducer semantics are unchanged: `apply_events` still sorts by `(ensure_utc(ts), event_id)`.
- Snapshot serialization is deterministic:
  - Decimals serialized as strings.
  - Datetimes serialized as timezone-aware ISO-8601 strings.
  - Symbol keys and fee currency keys are sorted.
  - Lots preserve stored order.

## Failure modes / fallback
- Missing checkpoint: full rebuild once from rowid `0`.
- Corrupt checkpoint JSON: ignored, full rebuild once, then checkpoint overwritten.
- Version mismatch: ignored, full rebuild once, then checkpoint overwritten with current version.

## Stage7 usage
Stage7 now calls `ledger_service.load_state_incremental(scope_id="stage7")` once per cycle and reuses the returned `LedgerState` for position updates and snapshot computations. This removes repeated full ledger replay in normal operation.
