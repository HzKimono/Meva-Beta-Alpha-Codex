# btcbot Runbook

## 1) Lint + tests (PowerShell)
```powershell
ruff check .
pytest -q
```

## 2) Doctor workflow
Doctor validates paths, safety gates, exchange rules usability for configured `SYMBOLS`, and replay/backtest readiness.

- If `--dataset` is omitted: doctor reports **OK** and states dataset is optional.
- If `--dataset` is provided and invalid: doctor reports **FAIL** and prints explicit ACTION lines.

```powershell
python -m btcbot.cli doctor --db .\btcbot_state.db
python -m btcbot.cli doctor --db .\btcbot_state.db --dataset .\data\replay
python -m btcbot.cli doctor --db .\btcbot_state.db --json
```

## 3) Replay dataset options
### Option A: Initialize deterministic synthetic dataset
Dataset bootstrap command: `python -m btcbot.cli replay-init --dataset .\data\replay --seed 123`.

```powershell
python -m btcbot.cli replay-init --dataset .\data\replay --seed 123
```

### Option B: Capture from public endpoints only
```powershell
python -m btcbot.cli replay-capture --dataset .\data\replay --symbols BTCTRY,ETHTRY --seconds 300 --interval-seconds 1
```

### Option C: Bring your own dataset
Create files that match `data/replay/README.md` contract.

## 4) Deterministic Stage7 backtest + parity + idempotency
```powershell
python -m btcbot.cli stage7-backtest --include-adaptation --dataset .\data\replay --out .\a.db --start "2026-01-01T00:00:00Z" --end "2026-01-01T00:05:00Z" --step-seconds 60 --seed 123
python -m btcbot.cli stage7-backtest --include-adaptation --dataset .\data\replay --out .\b.db --start "2026-01-01T00:00:00Z" --end "2026-01-01T00:05:00Z" --step-seconds 60 --seed 123
python -m btcbot.cli stage7-parity --out-a .\a.db --out-b .\b.db --start "2026-01-01T00:00:00Z" --end "2026-01-01T00:05:00Z" --include-adaptation
python -m btcbot.cli stage7-backtest --include-adaptation --dataset .\data\replay --out .\a.db --start "2026-01-01T00:00:00Z" --end "2026-01-01T00:05:00Z" --step-seconds 60 --seed 123
python -m btcbot.cli stage7-db-count --db .\a.db
```

## 5) Stage7 gate remains required
```powershell
$env:STAGE7_ENABLED="true"; python -m btcbot.cli stage7-run --dry-run --include-adaptation; Remove-Item Env:STAGE7_ENABLED
```


## 6) Exchange rules diagnostics (PowerShell)
```powershell
python scripts/capture_exchangeinfo_fixture.py
python -m btcbot.cli doctor --db .\btcbot_state.db --json
```

Doctor exchange-rules statuses:
- `PASS`: normalized rules usable (including verified conservative TRY min-notional fallback).
- `WARN`: explicit reason (for example non-TRY symbol missing min-notional) with `safe_behavior=reject_and_continue`.

## 7) BTCTurk live-reliability rollout (safe defaults)
Environment defaults are conservative:
- `BTCTURK_WS_ENABLED=false`
- `BTCTURK_REST_RELIABILITY_ENABLED=true`
- `BTCTURK_WS_IDLE_RECONNECT_MS=30000`
- `BTCTURK_WS_QUEUE_MAX=1000`
- `BTCTURK_REST_MAX_RETRIES=4`
- `BTCTURK_REST_BASE_DELAY_MS=400`
- `BTCTURK_REST_MAX_DELAY_MS=4000`
- `BTCTURK_RATE_LIMIT_RPS=8`
- `BTCTURK_RATE_LIMIT_BURST=8`
- `BTCTURK_MARKETDATA_MAX_AGE_MS=15000`

Rollout order:
1. Enable REST reliability only in dry-run.
2. Enable WS market data in dry-run.
3. Enable WS user stream finalization channels (`452`) and fill channels (`423`, `441`) in dry-run.
4. Live rollout with circuit breakers + reconcile loop continuously enabled.
