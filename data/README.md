# Data Directory

This directory is reserved for local replay/backtest datasets.

- Default replay dataset path: `./data/replay`
- Override path with env: `BTCTBOT_REPLAY_DATASET`
- Generate a deterministic starter dataset:
  - `python -m btcbot.cli replay-init --dataset .\data\replay --seed 123`

Large data files are intentionally ignored by git.
