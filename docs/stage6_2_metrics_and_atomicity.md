# Stage 6.2: Atomic Cycle State + Execution Metrics (Polished)

## 1) Transaction model and cursor semantics

Cycle processing uses **at-least-once fill fetching**:
- fill fetch uses a timestamp cursor with configured lookback,
- the same fill can be seen in later cycles.

Idempotency and atomic state writes are enforced as follows:
- `applied_fills(fill_id PRIMARY KEY)` ensures accounting effects are applied once,
- ledger dedupe (`INSERT OR IGNORE`) prevents duplicate fill/fee events,
- cursor advancement (`fills_cursor:*`) occurs only after successful write-state processing.

### Write transaction #1 (authoritative state)
The first write transaction contains only:
- ledger append (FILL/FEE events),
- accounting apply (including `applied_fills` idempotency writes),
- cursor advance (`fills_cursor:*`).

If any step fails, the transaction rolls back and cursor/state remain unchanged. This is the single commit boundary for authoritative trading state.

## 2) Ledger dedupe strategy

Ledger events are generated with stable IDs:
- fill event: `fill:{trade_id}` using `exchange_trade_id={trade_id}`
- fee event: `fee:{trade_id}` using `exchange_trade_id=fee:{trade_id}`

This keeps fill and fee namespaces separate while still enabling strict dedupe.

Cycle telemetry includes `ledger_events_attempted`, `ledger_events_inserted`, and `ledger_events_ignored`.

## 3) cycle_metrics persistence model

Stage 6.2 uses a **two-phase write model**:
1) authoritative transaction (required)
2) best-effort metrics transaction (optional)

Metrics are built after execution (final snapshot), and then one `cycle_metrics` row is upserted per `cycle_id` in its own small transaction.

If metrics persistence fails, `cycle_metrics_persist_failed` is logged and the already-committed authoritative state is not rolled back.

## 4) Metric semantics

DB column `fill_rate` is preserved for compatibility, but now explicitly stores:
- `fills_per_submitted_order = fills_count / orders_submitted`

Semantics are encoded in `meta_json`:
- `fill_rate_semantics: "fills_per_submitted_order"`

Numeric JSON conventions:
- money values remain strings (`fees_json`, `pnl_json`) for exactness,
- non-money metrics (e.g. slippage bps) are stored as floats.

## 5) Local validation

Run:
- `ruff format .`
- `ruff check .`
- `pytest -q`

Inspect SQLite tables:
- `ledger_events`
- `applied_fills`
- `cursors`
- `stage4_positions`
- `cycle_metrics`
