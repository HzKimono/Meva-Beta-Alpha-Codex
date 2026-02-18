Ledger Model (what is the source of truth)

- Stage3 accounting source of truth is **exchange fills**, ingested via `exchange.get_recent_fills(symbol)` and persisted with `INSERT OR IGNORE` on `fills(fill_id PRIMARY KEY)` in SQLite.
- Position state (`qty`, `avg_cost`, `realized_pnl`, `unrealized_pnl`, `fees_paid`) is a **derived state** updated from newly inserted fills.
- Unrealized PnL is recalculated from mark prices each refresh: `(mark - avg_cost) * qty` when mark > 0.
- For richer ledger math (Stage4/Stage7 paths), event-sourced ledger (`LedgerEvent`) is another source-of-truth layer with explicit lot matching and realized/unrealized computations.
- Practical interpretation:
  - Canonical persisted data: `fills` + `orders` + `positions` + idempotency tables in `StateStore`.
  - `positions` are materialized/derived and can be recomputed from fill history if needed.

Reconciliation Procedure (step-by-step)

1. **Cycle start / startup recovery**
   - `StartupRecoveryService.run()` calls `ExecutionService.refresh_order_lifecycle(symbols)` to reconcile local open/unknown orders with exchange open/all orders.
2. **Order lifecycle refresh**
   - Fetch open orders from exchange (`/api/v1/openOrders`) per symbol.
   - Update local order statuses for matched exchange orders.
   - For local orders not in open set, fetch `/api/v1/allOrders` in bounded window and attempt matching by order_id/client_order_id/fallback fields.
   - Unknown orders are reprobed on schedule; unresolved unknowns are kept with backoff metadata.
3. **Submit-time uncertain outcome handling**
   - On uncertain submit errors, execution runs `_reconcile_submit(...)`:
     - check open orders by `client_order_id`,
     - check allOrders window,
     - fallback field matching.
   - If confirmed -> persist committed order.
   - If not confirmed -> persist synthetic `unknown:{client_order_id}` with `OrderStatus.UNKNOWN` and idempotency status `UNKNOWN`.
4. **Fill ingestion reconciliation**
   - `AccountingService.refresh()` polls recent fills from exchange and writes only unseen fill_ids (`INSERT OR IGNORE`).
   - For newly inserted fills, `_apply_fill()` mutates position/avg-cost/realized fee fields.
5. **Mark-to-market pass**
   - After fill application, unrealized PnL is recomputed from provided cycle mark prices and persisted.
6. **Duplicate submit prevention / replay safety**
   - Execution reserves idempotency key before submit (`idempotency_keys` table, `(action_type,key)` PK).
   - Duplicate reservations short-circuit; stale pending reservations trigger recovery path.
   - Finalization transitions idempotency state to `COMMITTED|FAILED|UNKNOWN|SIMULATED`.

Known Edge Cases (list) + whether covered

- Duplicate fill payloads from exchange
  - Covered: **Yes** (`fills.fill_id` primary key + `INSERT OR IGNORE`).
- Partial fills
  - Covered: **Partial**.
  - Exchange order status mapping includes PARTIAL; position accounting handles incremental fills naturally as independent fill rows.
- Oversell inconsistency
  - Covered: **Partial**.
  - Stage3 `_apply_fill` caps sell quantity to current position (`sell_qty=min(position.qty, fill.qty)`), avoiding negative qty but potentially masking upstream inconsistency.
  - Ledger engine (`domain/ledger.py`) is stricter and raises `oversell_invariant_violation`.
- Non-quote fee asset (e.g., fee in base/third asset)
  - Covered: **No (correctness gap)**.
  - Stage3 accounting logs warning and ignores non-quote fee in PnL.
- Unknown submit outcome (network/server uncertainty)
  - Covered: **Yes**.
  - Reconciliation by open/all orders + UNKNOWN state + later reprobe/recovery.
- Duplicate order submission
  - Covered: **Yes (local scope)**.
  - Idempotency reservation + action dedupe + unique client_order_id handling in `orders` table.
- Out-of-order fills / timestamps
  - Covered: **Limited**.
  - Stage3 accounting applies fills in polling order; no explicit per-symbol temporal sort before `_apply_fill`.
  - Ledger engine sorts by `(ts,event_id)`.
- Rounding/quantization mismatch between intent and exchange execution
  - Covered: **Mostly for submission path** (risk/execution quantization before submit).
  - Fill accounting uses exact returned decimals; no re-quantization.

Required Fixes (ranked)

1) **Critical — handle non-quote fee assets in Stage3 accounting**
- Problem: ignoring non-quote fees understates fees and overstates realized/net PnL.
- Fix: convert fee currency to quote (TRY) using deterministic conversion source at fill timestamp (or mark proxy with explicit uncertainty flag).
- Files: `src/btcbot/accounting/accounting_service.py`, optionally `src/btcbot/services/market_data_service.py`.

2) **High — unify strictness between Stage3 accounting and ledger invariants**
- Problem: Stage3 silently truncates oversell (`min(position.qty, fill.qty)`), while ledger path fails hard on oversell invariant.
- Fix: add configurable invariant mode in Stage3 (warn/fail/observe-only) and emit explicit anomaly event when fill quantity exceeds tracked position.
- Files: `src/btcbot/accounting/accounting_service.py`, `src/btcbot/services/startup_recovery.py`, `src/btcbot/services/state_store.py`.

3) **High — enforce deterministic fill ordering before position mutation**
- Problem: exchange fill ordering assumptions are implicit; out-of-order application can distort avg_cost/realized PnL.
- Fix: sort newly fetched fills by `(ts, fill_id)` before `_apply_fill`.
- Files: `src/btcbot/accounting/accounting_service.py`.

4) **Medium — strengthen reconciliation escalation policy**
- Problem: repeated lifecycle refresh exceptions currently continue; drift can persist.
- Fix: count consecutive reconcile failures per symbol and force observe-only/kill-switch after threshold.
- Files: `src/btcbot/services/execution_service.py`, `src/btcbot/cli.py`.

5) **Medium — unify idempotency implementations across Stage3 vs OMS/Stage7 paths**
- Problem: Stage3 execution idempotency and OMS idempotency patterns are parallel but separate, increasing maintenance drift.
- Fix: extract common idempotency state machine contract and shared helper layer.
- Files: `src/btcbot/services/execution_service.py`, `src/btcbot/services/oms_service.py`, `src/btcbot/services/state_store.py`.

6) **Medium — add accounting correctness regression tests for edge cases**
- Problem: documented behaviors exist; need lock-in tests for non-quote fees, out-of-order fills, oversell, duplicate fills.
- Fix: add deterministic unit tests with explicit expected PnL/avg-cost trajectories.
- Files: `tests/` (new accounting-focused test modules).
