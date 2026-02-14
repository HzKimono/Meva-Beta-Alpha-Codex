# Stages and Gates

## Stage1-Stage3
- Core loop, strategy, and persistence foundations.
- Safety gates centered on dry-run defaults and policy checks.

## Stage4
- Expanded cycle runner and accounting/risk integration.
- Still constrained by kill-switch/live-arming policy.

## Stage5
- Strategy hardening and portfolio intent quality improvements.

## Stage6
- Risk-budget/degrade/anomaly coverage and metrics atomicity.

## Stage7 (current)
Definition of done:
- Deterministic replay/backtest support.
- Dry-run OMS lifecycle with full trace persistence.
- Adaptation pipeline behind explicit include/exclude controls.
- Parity fingerprinting for reproducibility checks.

Mandatory gates:
- `STAGE7_ENABLED=true`
- `DRY_RUN=true`
- `LIVE_TRADING=false`
- Kill-switch behavior remains active and blocks writes when enabled.

Disabled by design:
- Live trading/private endpoint order placement in Stage7 commands.
