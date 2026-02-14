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
