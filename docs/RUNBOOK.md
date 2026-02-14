# btcbot Runbook

## Quick checks (PowerShell)
```powershell
python -m btcbot.cli doctor --db .\btcbot_state.db --dataset .\data
```

```powershell
python -m btcbot.cli stage7-run --dry-run
```

## Backtest and parity (PowerShell)
```powershell
python -m btcbot.cli stage7-backtest --dataset .\tests\fixtures\sample_data --out .\out_a.db --start 2024-01-01T00:00:00Z --end 2024-01-01T00:10:00Z --include-adaptation
python -m btcbot.cli stage7-backtest --dataset .\tests\fixtures\sample_data --out .\out_b.db --start 2024-01-01T00:00:00Z --end 2024-01-01T00:10:00Z --include-adaptation
python -m btcbot.cli stage7-parity --out-a .\out_a.db --out-b .\out_b.db --start 2024-01-01T00:00:00Z --end 2024-01-01T00:10:00Z --include-adaptation
```

## Troubleshooting
- **doctor FAIL**: fix each reported item, then rerun doctor.
- **Kill switch block**: if `KILL_SWITCH=true`, write side effects stay blocked by design.
- **Stage7 gate failure**: ensure `STAGE7_ENABLED=true`, `DRY_RUN=true`, `LIVE_TRADING=false`.
- **Missing parity tables**: use Stage7 backtest DB outputs; parity prints a deterministic warning and still produces a stable fingerprint.
- **Dataset issues**: ensure replay folder has `candles`, `orderbook`, and `ticker` subfolders.

## Log filtering (PowerShell)
JSON logs are printed one event per line. Filter warnings/errors:
```powershell
python -m btcbot.cli stage7-run --dry-run | Select-String '"level": "WARNING"|"level": "ERROR"'
```
