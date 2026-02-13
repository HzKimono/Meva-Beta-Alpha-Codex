# Stage 7 PR-1 Foundation

## Scope
Stage 7 adds a dry-run-only accounting foundation that measures net-of-fees performance and persists a full cycle decision trace.

## What is measured (net-of-fees)
The Stage 7 ledger snapshot writes:
- `gross_pnl_try`
- `realized_pnl_try`
- `unrealized_pnl_try`
- `net_pnl_try`
- `fees_try`
- `slippage_try`
- `turnover_try`
- `equity_try`
- `max_drawdown`

Net PnL is deterministic in dry-run and includes fee deduction effects in realized PnL.

## Dry-run fill simulation
`stage7-run` reuses the existing cycle flow and then simulates fills only in dry-run:
1. Baseline fill price is mark price from market data (mid by default).
2. Slippage is applied using `STAGE7_SLIPPAGE_BPS`.
3. Fees are applied using `STAGE7_FEES_BPS` and booked as fee ledger events.
4. Fills + fees are persisted to the ledger events table.

## Persistence and tables
New tables:
- `stage7_cycle_trace`
  - cycle id, timestamp
  - selected universe
  - intents summary
  - mode payload (base/override/final)
  - per-order decisions with reasons
- `stage7_ledger_metrics`
  - one row per cycle id containing ledger snapshot metrics

Writes are performed atomically in a single transaction via `StateStore.save_stage7_cycle`.

## CLI
New command:

```bash
python -m btcbot.cli stage7-run --dry-run
```

This command is intentionally dry-run only and does not enable live execution.

## Acceptance criteria for Stage 7 PR-1
- Dry-run cycle runs and persists one Stage 7 trace row + metrics row.
- Net-of-fees realized/unrealized metrics are computed deterministically.
- Drawdown metric is available.
- Final mode monotonicity is preserved (`final_mode` is never less restrictive than `base_mode`).
