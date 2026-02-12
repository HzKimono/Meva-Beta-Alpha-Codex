# Stage 4: Controlled Live Trading (Canary Hardening)

Stage 4 is isolated from Stage 3 runtime behavior and focuses on **safety, correctness, and accounting integrity**.

## Safety model (triple gate)

Live write calls are allowed only when all are true:

- `KILL_SWITCH=false`
- `DRY_RUN=false`
- `LIVE_TRADING=true`
- `LIVE_TRADING_ACK=I_UNDERSTAND`

`ExecutionService` enforces these gates at runtime for every action.


## Running Stage 4 from CLI

Install in editable mode, then use the dedicated runtime entrypoint:

```bash
.venv/bin/pip install -e .
btcbot stage4-run --dry-run
```

Live side effects are allowed only when all are true:

- `DRY_RUN=false`
- `KILL_SWITCH=false`
- `LIVE_TRADING=true`
- `LIVE_TRADING_ACK=I_UNDERSTAND`

Canary recommendation:
- keep `MAX_OPEN_ORDERS` low,
- keep `MAX_POSITION_NOTIONAL_TRY` low,
- validate reconcile + accounting outputs in dry-run before arming live mode.

## Stage 4 canary checklist

- Start with dry-run mode and verify idempotency + accounting outputs.
- Keep small notional limits (`MAX_POSITION_NOTIONAL_TRY`, `MAX_OPEN_ORDERS`).
- Verify cancellation path by exchange order id works before live submit.
- Monitor `cycle_audit` for integrity alerts (fee conversion missing, oversell, reconcile anomalies).
- Keep `KILL_SWITCH=true` in config templates; disable only per controlled run.

## Idempotency design

- **Orders:** `client_order_id` uniqueness in `stage4_orders` (partial unique index for non-null IDs).
- **Execution submit dedupe:** duplicate `client_order_id` is a no-op.
- **Fills:** `fill_id` uniqueness in `stage4_fills`; if exchange lacks a stable fill id, a deterministic composite id is derived.
- **Cursors:** per-symbol cursor in `cursors` table (`fills_cursor:<symbol>`).

## Stage 4 exchange adapter contract

- Stage 4 uses a dedicated `BtcturkHttpClientStage4` adapter wrapping the existing Stage 3 HTTP client.
- `list_open_orders` is Decimal-native and parses API payloads directly into Stage 4 `Order` (`price`/`qty` are `Decimal`); it does not bridge through Stage 3 float models.
- `submit_limit_order`, cancellation methods, fills, and exchange info are exposed with the `ExchangeClientStage4` signatures.

## Fill ID strategy

- When BTCTurk provides a reliable unique trade/fill id, the Stage 4 adapter forwards it.
- When no reliable unique id is available, the adapter derives a deterministic fallback id from order/timestamp/price/qty (never `orderClientId`).
- Fallback ids are stable across runs for idempotent persistence.

## Accounting semantics

- Positions use weighted-average cost.
- Oversell is treated as integrity failure and raises `AccountingIntegrityError`.
- Fee handling in Stage 4 minimal mode:
  - if `fee_asset == TRY`, fee is applied,
  - otherwise fee conversion is not performed and an audit marker is emitted.
- PnL snapshot:
  - `total_equity_try = try_cash + Σ(qty * mark_price)`
  - `realized_total_try = Σ(position.realized_pnl_try)`
  - `realized_today_try = realized_total_try - baseline_at_day_start`

## Reconcile behavior

- marks DB orders missing on exchange as `unknown_closed`
- imports exchange-only orders as `mode=external`
- emits `enrich_exchange_ids` when DB order has client id but missing exchange id
- separately tracks exchange orders without client ids

## Environment variables used by Stage 4

- `MAX_OPEN_ORDERS`
- `MAX_POSITION_NOTIONAL_TRY`
- `MAX_DAILY_LOSS_TRY`
- `MAX_DRAWDOWN_PCT`
- `FEE_BPS_MAKER`
- `FEE_BPS_TAKER`
- `SLIPPAGE_BPS_BUFFER`
- `TRY_CASH_TARGET`
- `TRY_CASH_MAX`
- `RULES_CACHE_TTL_SEC`
- `FILLS_POLL_LOOKBACK_MINUTES`

## Limitations

- Non-TRY fee conversion is not yet implemented in Stage 4 minimal mode.
- Replace execution is represented as a cancel+submit plan; atomic exchange-side replace is not assumed.
