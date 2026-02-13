# Stage 7 PR-1 Foundation

## Scope
Stage 7 adds a dry-run-only accounting foundation that measures gross and net performance and persists a full cycle decision trace.

## PnL definitions (authoritative)
The Stage 7 ledger snapshot writes:
- `realized_pnl_try`: realized PnL from fills (**pre-fees**)
- `unrealized_pnl_try`: mark-to-market unrealized PnL (**pre-fees**)
- `gross_pnl_try = realized_pnl_try + unrealized_pnl_try` (**pre-fees, pre-slippage**)
- `fees_try`: TRY fees plus converted non-TRY fees
- `slippage_try`: simulated slippage impact in TRY
- `net_pnl_try = gross_pnl_try - fees_try - slippage_try`
- `equity_try = cash_try + net_pnl_try`
- `turnover_try`
- `max_drawdown`

`net_pnl_try` is the net-of-fees/net-of-slippage metric. Realized/unrealized remain pre-fee accounting values.

## Dry-run fill simulation
`stage7-run` reuses the existing cycle flow and then simulates fills only in dry-run:
1. Baseline fill price is mark price from market data (mid by default).
2. Slippage is applied using `STAGE7_SLIPPAGE_BPS`.
3. Fees are applied using `STAGE7_FEES_BPS` and booked as fee ledger events.
4. Fills + fees are persisted to the ledger events table.
5. Simulated fill IDs are deterministic within a cycle: `s7:{cycle_id}:{client_order_id|exchange_order_id|idx}`.

## Mode/risk integration and gating
Stage 7 reads the latest persisted risk mode as `base_mode` and computes `final_mode` via mode combine rules.
- `OBSERVE_ONLY`: simulate nothing; decisions are recorded as skipped.
- `REDUCE_RISK_ONLY`: SELL-only simulation; BUY actions are skipped with reason.
- `NORMAL`: simulate as usual.

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

## CLI safety
Command:

```bash
python -m btcbot.cli stage7-run --dry-run
```

Hard safety gates:
- `STAGE7_ENABLED` must be true.
- Dry-run is required.
- Stage 7 remains dry-run only (no live execution path).

## Acceptance criteria for Stage 7 PR-1
- Dry-run cycle runs and persists one Stage 7 trace row + metrics row.
- Gross/net semantics are deterministic and consistent.
- Drawdown metric is available.
- Final mode monotonicity is preserved (`final_mode` is never less restrictive than `base_mode`).

## Universe Selection v1 (PR-2)

Stage 7 now computes a deterministic universe at the start of each dry-run cycle.

### Scoring inputs
- **liquidity_score**: normalized 24h quote volume (`volume` from ticker stats).
- **spread_score**: normalized inverse of top-of-book spread bps using best bid/ask.
- **volatility_score**: normalized inverse volatility from recent closes (candles); if unavailable,
  fallback uses ticker high/low vs last, then daily percent change.
- **total_score**: weighted sum of the three component scores.

Default weights (normalized if custom values are provided):
- liquidity: `0.50`
- spread: `0.30`
- volatility: `0.20`

### Deterministic rules
- No randomness.
- Symbols are canonicalized using `canonical_symbol`.
- Per-cycle caching is used for orderbooks/candles to avoid duplicate network calls.
- Missing metric values get a deterministic penalty score of `-1` for that component.
- Final ordering is stable and deterministic:
  1. `total_score` descending
  2. `liquidity_score` descending
  3. `symbol` ascending

### Settings
- `STAGE7_UNIVERSE_SIZE` (default: `20`)
- `STAGE7_UNIVERSE_QUOTE_CCY` (default: `TRY`)
- `STAGE7_UNIVERSE_WHITELIST` (optional list)
- `STAGE7_UNIVERSE_BLACKLIST` (optional list)
- `STAGE7_MIN_QUOTE_VOLUME_TRY` (default: `0`)
- `STAGE7_MAX_SPREAD_BPS` (default: very high, effectively permissive)
- `STAGE7_VOL_LOOKBACK` (default: `20`)
- `STAGE7_SCORE_WEIGHTS` (optional dict with `liquidity`, `spread`, `volatility`)

### Persistence
- `stage7_cycle_trace.selected_universe_json`: selected ranked symbols for the cycle.
- `stage7_cycle_trace.universe_scores_json`: top scored candidates and score breakdown.

Universe selection is read-only and does not place orders. Stage 7 remains dry-run only.
