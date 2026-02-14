# btcbot Architecture

## Inventory and diagnosis
Current strengths:
- Domain models are mostly pure and concentrated under `src/btcbot/domain`.
- Stage7 persistence already uses atomic cycle writes (`StateStore.save_stage7_cycle`).
- Replay/backtest and parity paths are deterministic-first and well-tested.

Architecture smells addressed in this PR:
- Doctor checks were embedded in CLI and mixed parsing/validation/policy concerns.
- Safety gate semantics were documented inconsistently across docs.
- Missing top-level architecture/runbook/stage contract docs made onboarding slower.

## Module boundaries
- `btcbot.domain`: pure models/value objects and invariants, no IO.
- `btcbot.services`: orchestration and business workflows (cycle runners, risk, adaptation, parity, doctor checks).
- `btcbot.adapters`: exchange and HTTP integrations.
- `btcbot.cli`: command entrypoint, argument wiring, and output formatting.
- `btcbot.logging_utils`: JSON logging and logger-level setup.

## Stage7 lifecycle (end-to-end)
1. **Input data**: live adapters or `MarketDataReplay`.
2. **Selection & planning**: universe selection + portfolio policy + intent generation.
3. **Risk/degrade gates**: risk decision (`NORMAL/REDUCE_RISK_ONLY/OBSERVE_ONLY`) combined with global safety gates.
4. **Dry-run execution state machine**: deterministic OMS transitions and simulated fills only.
5. **Persistence**: cycle trace + ledger metrics + run metrics + optional adaptation data in a single transaction.
6. **Metrics/parity**: deterministic fingerprints over canonicalized DB rows.

## Determinism principles
- Replay clock is sourced from dataset timestamps; no wall-clock dependency for backtests.
- Stable ordering for parity payloads and JSON serialization.
- `Decimal` values persisted as strings for round-trip stability.
- Missing parity tables produce deterministic empty fingerprints instead of runtime failure.

## Safety model
- Stage7 is dry-run only.
- `STAGE7_ENABLED` requires `DRY_RUN=true` and `LIVE_TRADING=false`.
- Kill-switch remains authoritative for blocking writes in run paths.
