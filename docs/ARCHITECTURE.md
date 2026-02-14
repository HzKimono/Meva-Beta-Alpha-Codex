# btcbot Architecture

## Replay architecture
- `btcbot.services.market_data_replay` consumes replay folder files (`candles/`, `orderbook/`, optional `ticker/`) and provides deterministic market snapshots.
- `btcbot.adapters.replay_exchange` wraps replay service behind exchange client interfaces.
- `btcbot.replay.validate` defines and enforces the replay dataset contract for CLI doctor/tooling.
- `btcbot.replay.tools` provides:
  - `replay-init` (folder + schema + deterministic synthetic sample)
  - `replay-capture` (public endpoint capture only; atomic file writes)

## Dataset contract (formal)
- Required folders: `candles`, `orderbook`
- Optional folder: `ticker`
- Required candle columns: `ts,open,high,low,close,volume`
- Required orderbook columns: `ts,best_bid,best_ask`
- Required ticker columns when present: `ts,last,high,low,volume` (`quote_volume` optional)
- Timestamps: parseable ISO8601 / unix sec / unix ms; monotonic per file

## Determinism and idempotency guarantees
- Replay backtest is deterministic for same dataset/time-range/seed.
- Parity compares deterministic fingerprints over persisted Stage7 tables.
- Rerunning Stage7 backtest into the same DB is idempotent for cycle rows.

## Safety boundaries
- Stage7 gate semantics unchanged: `STAGE7_ENABLED` still gates `stage7-run`.
- Kill-switch semantics unchanged: `KILL_SWITCH=true` keeps side effects blocked/observe-only.
- `replay-capture` uses public endpoints only and does not call private trading endpoints.
