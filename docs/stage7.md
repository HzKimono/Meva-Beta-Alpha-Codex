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

## PR-5 Risk Budget v2 + Exposure Tracking

Stage 7 now computes a dedicated **risk decision before universe selection** and persists it in both cycle trace and a dedicated table.

### New concepts
- `RiskMode`: `NORMAL | REDUCE_RISK_ONLY | OBSERVE_ONLY`
- `RiskDecision`: mode, structured reasons, cooldown, decided timestamp, deterministic inputs hash.
- `ExposureSnapshot`: per-symbol and total TRY exposure, concentration top-N, turnover estimate, free cash, deterministic inputs hash.

### Rules (deterministic)
- Drawdown breach (`STAGE7_MAX_DRAWDOWN_PCT`) -> `OBSERVE_ONLY` (+ cooldown).
- Daily loss breach (`STAGE7_MAX_DAILY_LOSS_TRY`) -> `OBSERVE_ONLY`.
- Consecutive losses (`STAGE7_MAX_CONSECUTIVE_LOSSES`) -> configurable degrade mode.
- Stale data (`STAGE7_MAX_DATA_AGE_SEC`) -> `OBSERVE_ONLY`.
- Spread spike (`STAGE7_SPREAD_SPIKE_BPS`) or liquidity drop (`STAGE7_MIN_QUOTE_VOLUME_TRY`) -> `REDUCE_RISK_ONLY`.
- Cooldown monotonicity keeps prior restrictive mode until cooldown expiry.

### Persistence
- New table: `stage7_risk_decisions` (append-only).
- `stage7_cycle_trace.mode_json` now includes:
  - `risk_mode`, `risk_reasons`, `risk_cooldown_until`, `risk_inputs_hash`.

### New Stage 7 settings
- `STAGE7_MAX_DRAWDOWN_PCT`
- `STAGE7_MAX_DAILY_LOSS_TRY`
- `STAGE7_MAX_CONSECUTIVE_LOSSES`
- `STAGE7_MAX_DATA_AGE_SEC`
- `STAGE7_SPREAD_SPIKE_BPS`
- `STAGE7_RISK_COOLDOWN_SEC`
- `STAGE7_CONCENTRATION_TOP_N`
- `STAGE7_LOSS_GUARDRAIL_MODE` (`reduce_risk_only|observe_only`)

Stage 7 remains **DRY-RUN ONLY**.

## PR-6 OMS / Execution v1 (DRY-RUN state machine)

PR-6 adds a deterministic, idempotent OMS state machine for Stage 7 intents. Stage 7 remains **DRY-RUN ONLY**: no private endpoint calls and no real order placement.

### OMS lifecycle
For each non-skipped `OrderIntent`, OMS transitions through dry-run states:
- `PLANNED -> SUBMITTED -> ACKED -> (PARTIALLY_FILLED)? -> (FILLED | REJECTED | CANCELED)`

Reject simulation rules in v1:
- skipped intents do not enter OMS.
- `qty <= 0` or `price <= 0` => `REJECTED`.

### Deterministic identity
- `order_id = s7o:<short_hash(client_order_id)>`
- `event_id = s7e:<short_hash(client_order_id:seq:event_type)>`

All quantity/price values use Decimal and persisted as strings.

### Persistence
New table: `stage7_orders`
- deterministic order identity and current status snapshot.
- includes `filled_qty`, `avg_fill_price_try`, `intent_hash`, `last_update`.

New table: `stage7_order_events` (append-only)
- deterministic `event_id` primary key for idempotent replay safety.
- payload stored as stable sorted JSON.

Indexes:
- `stage7_orders(client_order_id)`
- `stage7_order_events(client_order_id, ts)`

### Idempotency / dedupe
- `client_order_id` is unique in `stage7_orders`.
- Re-running the same cycle with the same intents does not duplicate orders/events.
- State transitions are emitted exactly once per `client_order_id` transition.

### Runner integration
- In `OBSERVE_ONLY`, OMS processing is skipped.
- Otherwise, Stage 7 processes planned intents through OMS and stores:
  - `oms_summary` counts by status
  - `events_total`

## PR-7 OMS / Execution v2 Reliability Hardening (DRY-RUN)

PR-7 hardens the Stage 7 OMS for restart safety and deterministic reliability behavior while remaining **DRY-RUN ONLY**.

### Idempotency model
- `client_order_id` remains the primary dedupe key for order intent processing.
- New table `stage7_idempotency_keys(key, ts, payload_hash)` persists action-level idempotency.
- Lifecycle actions (`submit/cancel/replace`) are guarded by idempotency key registration.
- Reused key with same payload is treated as duplicate and ignored (`DUPLICATE_IGNORED` event).
- Reused key with different payload hash raises conflict and emits `IDEMPOTENCY_CONFLICT`.

### Event-log source of truth
- `stage7_order_events` remains append-only and guarded by unique `event_id` PK.
- State transitions are constrained by an allowed transition graph to prevent out-of-order corruption.
- `stage7_orders` is the latest snapshot view derived consistently from event-driven transitions.

### Retry / backoff policy
- Transient errors retried with deterministic exponential backoff + seeded jitter:
  - transient: `NetworkTimeout`, `RateLimitError`, `TemporaryUnavailable`
  - non-retryable: all others (including explicit non-retryable adapter failures)
- Retry policy settings:
  - `STAGE7_RETRY_MAX_ATTEMPTS`
  - `STAGE7_RETRY_BASE_DELAY_MS`
  - `STAGE7_RETRY_MAX_DELAY_MS`
- Retry lifecycle events:
  - `RETRY_SCHEDULED` for each retry attempt
  - `RETRY_GIVEUP` when retry budget is exhausted

### Throttling guardrails
- Token-bucket throttling is applied before submit/cancel/replace:
  - `STAGE7_RATE_LIMIT_RPS`
  - `STAGE7_RATE_LIMIT_BURST`
- If throttled, OMS records `THROTTLED` with `next_eligible_ts` and defers processing.

### Crash-recovery and rerun safety
- New `reconcile_open_orders()` reloads non-terminal orders, replays/resumes pending actions, and continues processing safely.
- Running `stage7-run` repeatedly does not duplicate submissions/events due to:
  - idempotency key checks
  - event-id dedupe
  - transition guards

Stage 7 safety gates are unchanged: `STAGE7_ENABLED` requires `DRY_RUN=true` and `LIVE_TRADING=false`.

## PR-8 Monitoring / Observability

Stage 7 now persists per-cycle run metrics in `stage7_run_metrics` for queryable reporting and export.

### Metrics glossary
- `net_pnl_try`: net PnL after fees/slippage.
- `turnover_try`: total traded notional across the cycle.
- `max_drawdown_pct`: max observed drawdown ratio for the simulated equity path.

### Storage
- Trace: `stage7_cycle_trace`
- Ledger summary: `stage7_ledger_metrics`
- Observability run metrics + alerts: `stage7_run_metrics`

### CLI hooks
- `btcbot stage7-report --last N`
- `btcbot stage7-export --last N --format jsonl|csv --out <path>`
- `btcbot stage7-alerts --last N`

### Alert flags
Computed deterministically per cycle (`alert_flags` payload):
- `drawdown_breach` when `max_drawdown_pct >= STAGE7_MAX_DRAWDOWN_PCT`
- `reject_spike` when `oms_rejected_count >= STAGE7_REJECT_SPIKE_THRESHOLD`
- `missing_data` when missing mark prices are observed
- `throttled` when throttling events are observed
- `retry_excess` when retry count exceeds `STAGE7_RETRY_ALERT_THRESHOLD`

Related quality signal (`quality_flags` payload):
- `missing_mark_price` when any order/action path lacks a resolved mark.

## PR-9 Parameter Adaptation

Stage7 now supports deterministic parameter adaptation for dry-run cycles only.

- Adaptable knobs: universe size, score weights, order offset bps, turnover cap TRY, max orders/cycle, max spread bps, cash target TRY, min quote volume TRY (driven by run `alert_flags` and `quality_flags.missing_mark_price`).
- Strict bounds are enforced before any proposal can be applied:
  - universe size `[5, 50]`
  - score weights each `[0,1]`, deterministically normalized to sum `1`
  - order offset bps `[0,50]`
  - turnover cap TRY `[0, NOTIONAL_CAP_TRY_PER_CYCLE]`
  - max orders/cycle `[1,20]`
  - max spread bps `[10,500]`
  - cash target TRY `[0, TRY_CASH_MAX]`
- Apply conditions:
  - only in `NORMAL` mode
  - rejected when breach flags are present
  - rejected in `OBSERVE_ONLY` and `REDUCE_RISK_ONLY`
- Rollback triggers:
  - drawdown breach
  - reject spike
  - persistent throttling window
  - optional consecutive net PnL floor breach
- Audit persistence:
  - `stage7_params_active`
  - `stage7_param_changes`
  - `stage7_params_checkpoints`
  - plus `stage7_cycle_trace.active_param_version` and `stage7_cycle_trace.param_change_json`

Example inspection queries:

```sql
SELECT * FROM stage7_params_active;
SELECT * FROM stage7_param_changes ORDER BY ts DESC LIMIT 20;
SELECT * FROM stage7_params_checkpoints ORDER BY version DESC;
SELECT cycle_id, active_param_version, param_change_json FROM stage7_cycle_trace ORDER BY ts DESC LIMIT 20;
```

## PR-10 Backtest Harness Parity (DRY-RUN ONLY)

PR-10 adds an offline replay/backtest harness that reuses Stage 7 services and persists into the same Stage 7 schema.

### Data format (folder option)
Replay loader expects:
- `data/candles/<SYMBOL>.csv` with: `ts,open,high,low,close,volume`
- `data/orderbook/<SYMBOL>.csv` with: `ts,best_bid,best_ask`
- `data/ticker/<SYMBOL>.csv` with: `ts,last,high,low,volume,quote_volume` (`quote_volume` optional)

Schema is strict and deterministic (invalid/missing required headers fail fast).

### Replay stepping
- Fixed `step_seconds` timeline between `start` and `end`.
- `now()` is controlled by replay clock only.
- `advance()` moves exactly one step and returns `False` when done.
- Missing points use deterministic nearest-prior carry-forward.

### Reproducibility guarantees
- Backtest is DRY-RUN only.
- Deterministic cycle IDs in backtest: `bt:<YYYYmmddHHMMSS>:<idx>`.
- No `datetime.now()` in replay cycles; time source is replay clock.
- Seed is accepted and persisted in run metadata path, but deterministic rules prefer stable ordering/carry-forward over randomness.
- Params are frozen by default and adaptation is disabled by default (`freeze_params=True`, `disable_adaptation=True`).

### CLI
Run replay backtest (PowerShell-friendly aliases supported):
```bash
python -m btcbot.cli stage7-backtest \
  --dataset ./data \
  --out ./backtest.db \
  --start 2024-01-01T00:00:00Z \
  --end 2024-01-01T01:00:00Z \
  --step-seconds 60 \
  --seed 123
```

Compare two runs (aliases `--out-a/--out-b` also supported):
```bash
python -m btcbot.cli stage7-parity \
  --out-a ./run_a.db \
  --out-b ./run_b.db \
  --start 2024-01-01T00:00:00Z \
  --end 2024-01-01T01:00:00Z
```

Export backtest rows (`stage7-backtest-report` is a compatibility alias):
```bash
python -m btcbot.cli stage7-backtest-report --db ./backtest.db --last 100 --format jsonl --out out.jsonl
```

Inspect Stage 7 table counts without external `sqlite3` binary:
```bash
python -m btcbot.cli stage7-db-count --db ./backtest.db
```

### Fingerprint mismatches
`stage7-parity` computes SHA-256 over deterministic per-cycle essentials:
- timestamp + cycle id
- base/final mode
- selected universe symbols
- net/fees/slippage/turnover
- intents count + filled/rejected counts

A mismatch means at least one of those canonical inputs diverged in the compared DB windows.
