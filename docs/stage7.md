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

## Portfolio Policy v1 (PR-3)

Stage 7 now computes a deterministic, explainable **portfolio plan** each cycle (dry-run only).

### Inputs
- Selected universe (from PR-2).
- Mark prices in TRY for selected symbols.
- Account balances (TRY cash + base-asset quantities).

### Constraints enforced
- TRY cash buffer target using `TRY_CASH_TARGET`, bounded by `TRY_CASH_MAX`.
- Per-symbol max target notional with `MAX_POSITION_NOTIONAL_TRY`.
- Per-cycle turnover cap with `NOTIONAL_CAP_TRY_PER_CYCLE`.
- Max action count with `MAX_ORDERS_PER_CYCLE`.
- Min action notional with `MIN_ORDER_NOTIONAL_TRY`.

### Policy logic (v1)
- Build portfolio snapshot (`cash_try`, positions, `equity_try`).
- Compute investable equity after cash target.
- Assign equal target weights over universe.
- Clip each symbol target by per-symbol max notional.
- Send leftover allocation to cash (implicit cash weight).

### Rebalance output
The plan contains:
- `target_weights` equivalent via per-symbol `weight` in `allocations`.
- `cash_target_try`.
- `actions` with deterministic SELL-first then BUY ordering, desired TRY notional, estimated qty, and reason. SELL-first is enforced during turnover allocation and max-order selection, not only in final display order.
- Trace skip reasons are explicit and deterministic (e.g. `min_notional`, `turnover_cap`, `max_orders`, `mode_reduce_risk_only`, `observe_only`, `missing_mark_price`).

Mode gating:
- `OBSERVE_ONLY`: build plan, but no actions.
- `REDUCE_RISK_ONLY`: SELL-only actions retained.

### Persistence
`stage7_cycle_trace` now also stores:
- `portfolio_plan_json`: full serialized plan + notes/constraints.

### Scope note
PR-3 only produces and persists a plan. It does **not** place live orders; execution belongs to PR-4+.

## PR-4 Order Intents (Dry-run only)

PR-4 converts `PortfolioPlan.actions` into deterministic, exchange-rule-compliant `OrderIntent` records.

### What is produced
- `OrderIntent` domain model with: cycle/symbol/side/type/price/qty/notional, deterministic `client_order_id`, reason, and skip metadata.
- LIMIT-only intents in this phase.
- Deterministic `client_order_id` format: `s7:{cycle_id}:{symbol}:{side}:{short_hash}` where hash input is `(cycle_id, symbol, side, price_try, qty, reason)`.

### Pricing and quantity rules
- Baseline price = Stage7 mark price.
- Offset: `STAGE7_ORDER_OFFSET_BPS`.
  - SELL: `mark * (1 + bps/10000)`
  - BUY: `mark * (1 - bps/10000)`
- Price quantized by exchange tick size.
- Qty = `target_notional_try / price`, then quantized by lot size.
- Pre-trade validation rejects:
  - `qty_rounds_to_zero`
  - `min_notional` (post-quantization)

### Mode gating
- `OBSERVE_ONLY`: no intents emitted.
- `REDUCE_RISK_ONLY`: BUY intents are dropped; SELL intents remain.

### Exchange metadata + fallbacks
`ExchangeRulesService` prefers adapter exchange metadata and falls back to deterministic Stage7 settings when metadata is missing:
- `STAGE7_RULES_FALLBACK_TICK_SIZE`
- `STAGE7_RULES_FALLBACK_LOT_SIZE`
- `STAGE7_RULES_FALLBACK_MIN_NOTIONAL_TRY`

Metadata hardening controls:
- `STAGE7_RULES_REQUIRE_METADATA` (default `true`): when enabled, missing/invalid metadata does not use fallback.
- `STAGE7_RULES_INVALID_METADATA_POLICY` (`skip_symbol` or `observe_only_cycle`):
  - `skip_symbol`: affected symbols generate skipped intents with `rules_unavailable:<status>`.
  - `observe_only_cycle`: any affected symbol forces Stage7 cycle final mode to `OBSERVE_ONLY`.

Cycle trace summary now includes `rules_stats` with fallback/missing/invalid counts and symbol lists.

### Persistence
New table: `stage7_order_intents`
- `client_order_id` PK
- cycle, ts, symbol, side, order type, price, qty, notional
- status (`PLANNED` or `SKIPPED`)
- full JSON payload (`intent_json`)

`stage7-run` now persists intents together with cycle trace/metrics atomically. No live order submission is performed in PR-4.
