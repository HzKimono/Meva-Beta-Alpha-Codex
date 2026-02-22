# Risk Budget Signal Pipeline

## Source of truth

Risk budget signal inputs are derived from persisted `pnl_snapshots` in SQLite (`StateStore.pnl_snapshots`).
`risk_decisions` and `risk_state_current` remain the persistence targets for risk decisions/state.

## Consecutive loss streak

- Input data: `realized_today_try` from most-recent `pnl_snapshots` rows.
- Query: `StateStore.list_pnl_snapshots_recent(limit)` (descending by `ts`).
- Definition:
  - loss day: `realized_today_try < 0`
  - win day: `realized_today_try > 0`
  - zero breaks streak and stops counting
- Output: count of consecutive losses from newest snapshot up to configured lookback.
- Fallback: if no/insufficient data, streak is `0`.

## Volatility regime

- Input data: `total_equity_try` from recent snapshots.
- Window: `STAGE7_VOL_LOOKBACK`.
- Returns: `ln(eq[i] / eq[i-1])` on chronological equity points (`eq > 0`).
- Volatility: standard deviation of returns, rounded to 8 decimals for deterministic classification.
- Classification thresholds:
  - `vol <= STAGE7_VOL_LOW_THRESHOLD` => `low`
  - `vol >= STAGE7_VOL_HIGH_THRESHOLD` => `high`
  - otherwise => `normal`
- Fallback: if fewer than 5 returns can be computed, regime defaults to `normal`.

## Runtime behavior

`RiskBudgetService.compute_decision()` computes both signals each cycle and feeds them to
`RiskBudgetPolicy.evaluate(consecutive_loss_streak=..., volatility_regime=...)`.
If signal fetch fails, the service fails closed to default (`streak=0`, `volatility_regime="normal"`) and continues.
