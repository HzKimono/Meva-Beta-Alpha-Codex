# Phase 6 Operator Notes: Canonical Ledger + P&L + Stage7 Reporting

## Canonical accounting source of truth

Phase 6 uses **domain ledger events reduced by `LedgerService`** as the canonical accounting path.

- Source events: `ledger_events` (`FILL`, `FEE`, `ADJUSTMENT`) in SQLite.
- Reducer/checkpoint: `LedgerService.load_state_incremental()` + `ledger_reducer_checkpoints`.
- Canonical financial outputs are derived from this reducer state:
  - `realized_pnl_try`
  - `unrealized_pnl_try`
  - `fees_try` (converted to TRY)
  - `slippage_try`
  - `gross_pnl_try`
  - `net_pnl_try = gross - fees - funding_cost - slippage` (funding_cost currently `0`)
  - `equity_try`
  - `turnover_try`
  - `max_drawdown_ratio`

`btcbot/accounting/ledger.py` remains non-canonical/auxiliary for now and must not be used to override Stage7 P&L outputs.

## Determinism + idempotency

- Event replay order is deterministic (`ORDER BY rowid` for incremental scans; ledger reducer re-sorts by `(ts,event_id)`).
- Event ingestion is idempotent (`INSERT OR IGNORE` over unique event keys).
- Checkpoint state and full replay state are expected to match exactly (covered by tests).

## Fee conversion semantics

- Fee conversion to TRY uses `PriceConverter(fee_ccy, "TRY")` and `amount * rate`.
- For Stage7 canonical snapshot computation, fee conversion is **strict/fail-closed**.
- If a non-TRY fee currency lacks a conversion rate, execution fails with a clear conversion error.

## Stage7 reporting/export

Commands:

- Human-readable + JSON summary:
  - `btcbot stage7-report --db ./btcbot_state.db --last 50`
- Machine export:
  - `btcbot stage7-export --db ./btcbot_state.db --last 50 --format json`
  - `btcbot stage7-export --db ./btcbot_state.db --last 50 --format csv`

Export/report payloads include `schema_version: "phase6-v1"` for schema stability.
