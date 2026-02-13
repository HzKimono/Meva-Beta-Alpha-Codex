# Stage 6.3 — Risk Budget & Mode Gating

Stage 6.3 introduces deterministic per-cycle risk mode selection and execution gating.

## Modes

- `NORMAL`: run normal execution path.
- `REDUCE_RISK_ONLY`: allow only risk-reducing actions.
  - cancels are allowed
  - new `BUY` submissions are blocked
  - `SELL` submissions are allowed
- `OBSERVE_ONLY`: no order submission/cancel side effects are executed.

## Decision policy

The mode is chosen by `decide_mode(limits, signals)` in priority order:

1. Drawdown breach (`drawdown_try >= max_drawdown_try`) or daily PnL breach (`daily_pnl_try <= -max_daily_drawdown_try`) => `OBSERVE_ONLY`, reason `DRAWDOWN_LIMIT`
2. Exposure breach (`gross_exposure_try > max_gross_exposure_try` or `largest_position_pct > max_position_pct`) => `REDUCE_RISK_ONLY`, reason `EXPOSURE_LIMIT`
3. Fee budget breach (`fees_try_today > max_fee_try_per_day` when configured) => `REDUCE_RISK_ONLY`, reason `FEE_BUDGET`
4. Otherwise => `NORMAL`, reason `OK`

Kill-switch remains authoritative and overrides final mode to `OBSERVE_ONLY` with reason `KILL_SWITCH`.

## Signals and limits

Signals include:

- equity, peak equity, drawdown (TRY)
- daily realized PnL (TRY)
- gross exposure (TRY)
- largest position as equity percentage
- today's TRY fees (single source from ledger PnL report fees)

Limits are configured via settings/env:

- `RISK_MAX_DAILY_DRAWDOWN_TRY`
- `RISK_MAX_DRAWDOWN_TRY`
- `RISK_MAX_GROSS_EXPOSURE_TRY`
- `RISK_MAX_POSITION_PCT`
- `RISK_MAX_ORDER_NOTIONAL_TRY`
- `RISK_MIN_CASH_TRY` (optional)
- `RISK_MAX_FEE_TRY_PER_DAY` (optional)

## Persistence and auditability

Per-cycle decision snapshots are persisted in `risk_decisions` (mode, reasons, signals, limits, previous mode).
Current rolling risk state (mode, rolling peak equity, fee-day state) is stored in `risk_state_current`. Peak equity is cumulative (no daily reset).

Each cycle emits a structured `risk_decision` log with:

- `cycle_id`
- `mode`
- `prev_mode`
- `reasons`
- key signals: drawdown, gross exposure, fees today
