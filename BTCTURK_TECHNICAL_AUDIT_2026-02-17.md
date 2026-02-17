- Module Map (table)

| Category | Primary modules/packages | What is here |
|---|---|---|
| exchange | `src/btcbot/adapters/btcturk_http.py`, `src/btcbot/adapters/btcturk/`, `src/btcbot/adapters/exchange*.py`, `src/btcbot/services/exchange_factory.py` | BTCTurk REST/WS adapters, exchange interfaces, dry-run/live client construction |
| strategy | `src/btcbot/strategies/`, `src/btcbot/services/strategy_service.py`, `src/btcbot/domain/strategy_core.py` | Strategy implementations (`profit_v1`, baseline), strategy context, intent generation |
| risk | `src/btcbot/risk/`, `src/btcbot/services/risk_service.py`, `src/btcbot/services/risk_policy.py`, `src/btcbot/services/stage7_risk_budget_service.py` | Stage3 risk filtering, Stage4 risk policy, Stage7 risk budgeting/guardrails |
| execution | `src/btcbot/services/execution_service.py`, `src/btcbot/services/execution_service_stage4.py`, `src/btcbot/services/oms_service.py` | Order submit/cancel lifecycle, idempotency, reconciliation, stage-specific execution |
| data | `src/btcbot/services/market_data_service.py`, `src/btcbot/services/market_data_replay.py`, `src/btcbot/replay/`, `data/` | Market data access/freshness, replay dataset capture/validation, fixtures |
| infra | `src/btcbot/config.py`, `src/btcbot/services/process_lock.py`, `src/btcbot/observability*.py`, `src/btcbot/logging_*.py`, `src/btcbot/security/` | Config/env validation, single-instance locking, metrics/tracing, logging context, secret handling |
| utils | `src/btcbot/services/retry.py`, `src/btcbot/services/client_order_id_service.py`, `scripts/` | Retry helpers, deterministic client order IDs, repo maintenance/debug scripts |
| persistence/state | `src/btcbot/services/state_store.py`, `src/btcbot/accounting/accounting_service.py`, `src/btcbot/services/startup_recovery.py` | SQLite schema + state transitions, fills/positions accounting, restart recovery |
| domain/contracts | `src/btcbot/domain/` | Typed models, enums, symbols/rules, lifecycle/order/risk domain objects |
| tests | `tests/`, `tests/chaos/`, `tests/soak/`, `tests/fixtures/` | Unit/integration-style tests, chaos/resilience scenarios, fixtures |
| docs | `README.md`, `docs/`, `*_AUDIT*.md`, `*_REPORT*.md` | Architecture, operations, audit and quality-gate docs |

- Entrypoints

- CLI/package entrypoints:
  - `src/btcbot/__main__.py` -> `btcbot.cli:main`.
  - `pyproject.toml` exposes console script `btcbot = "btcbot.cli:main"`.
- CLI commands / runtime modes (in `src/btcbot/cli.py`):
  - `run`: Stage3 cycle runner (single cycle or loop `--loop`).
  - `canary once|loop`: constrained canary flows.
  - `stage4-run`: Stage4 cycle runner (single/loop).
  - `stage7-run`: Stage7 dry-run cycle.
  - `health`, `doctor`.
  - Stage7 reporting/export/alerts/backtest/parity/db-count commands.
  - Replay tooling: `replay-init`, `replay-capture`.
- Runtime orchestration entry services:
  - Stage3: `run_stage3_runtime()` -> `run_with_optional_loop()` -> `run_cycle()`.
  - Stage4: `run_cycle_stage4()` -> `Stage4CycleRunner.run_one_cycle()`.
  - Stage7: `run_cycle_stage7()` -> `Stage7CycleRunner.run_one_cycle()`.

- Dependency Graph (bullets)

- Package-level import edges (observed from `src/btcbot/**/*.py`):
  - `cli -> services, config, adapters, risk, strategies, accounting, security, observability`.
  - `services -> domain` (heaviest edge), plus `services -> adapters, config, risk, strategies, accounting`.
  - `risk -> domain, services`.
  - `strategies -> domain, config`.
  - `adapters -> domain, services, observability, security`.
  - `accounting -> domain, adapters, services`.
  - `__main__ -> cli`.
- Suspected cyclic dependencies (package level):
  - `services <-> risk` (both directions exist).
  - `services <-> accounting` (both directions exist).
  - No direct cycle detected between `cli` and other top-level packages (primarily outbound from CLI).
- Notes:
  - These are package-level cycles; whether they are harmful depends on module-level import timing.
  - Stage-specific flows (Stage4/Stage7) concentrate much of the cross-package coupling in `services/`.

- Navigation Index

- Placing orders:
  - Start: `src/btcbot/services/execution_service.py` (`execute_intents`, `cancel_stale_orders`).
  - Exchange boundary: `src/btcbot/adapters/btcturk_http.py` (`place_limit_order`, cancel/get orders).
  - Client construction: `src/btcbot/services/exchange_factory.py`.
  - CLI wiring: `src/btcbot/cli.py` (`run_cycle`).
- Receiving market data:
  - Start: `src/btcbot/services/market_data_service.py`.
  - REST source: exchange client calls via `ExchangeClient.get_orderbook`.
  - WS adapter implementation: `src/btcbot/adapters/btcturk/ws_client.py` (async queue/task model).
- Strategy decision:
  - Start: `src/btcbot/services/strategy_service.py`.
  - Strategy implementation: `src/btcbot/strategies/profit_v1.py` (current Stage3 default).
  - Domain intent model: `src/btcbot/domain/intent.py`.
- Risk checks:
  - Stage3 path: `src/btcbot/services/risk_service.py` + `src/btcbot/risk/policy.py`.
  - Stage4 path: `src/btcbot/services/risk_policy.py`.
  - Stage7 risk budget: `src/btcbot/services/stage7_risk_budget_service.py`.
- Persistence/state:
  - Start: `src/btcbot/services/state_store.py` (SQLite schema + CRUD + idempotency).
  - Accounting persistence effects: `src/btcbot/accounting/accounting_service.py`.
  - Restart recovery hook: `src/btcbot/services/startup_recovery.py`.
- Scheduling/async orchestration:
  - Sync scheduler loop: `src/btcbot/cli.py` (`run_with_optional_loop`).
  - Single-instance runtime lock: `src/btcbot/services/process_lock.py`.
  - Async WS orchestration (not default Stage3 loop): `src/btcbot/adapters/btcturk/ws_client.py`.

- Missing Components (bullets)

- Logging:
  - Present: structured logging + context helpers.
  - Potential gap: no explicit centralized log schema/versioning contract document for all stages.
- Idempotency:
  - Present: SQLite-backed idempotency keys + dedupe keys + recovery flow.
  - Potential gap: cross-process/multi-host global idempotency store is absent (single-DB scope).
- Retry:
  - Present: retry/backoff helpers in adapters/services.
  - Potential gap: fixed jitter seed usage in some paths can correlate retries across replicas.
- Circuit breaker:
  - Partial: kill-switch/safe-mode/observe-only gating exists.
  - Gap: no explicit generalized circuit-breaker component (stateful open/half-open/closed) per external dependency.
- Rate limit:
  - Present in async adapter components (`btcturk/rate_limit.py`, token bucket usage).
  - Potential gap: active Stage3 sync path does not visibly use the async limiter abstraction.
- Secrets:
  - Present: secret provider injection + controls + redaction.
  - Potential gap: repo does not show external secret manager integration artifact (deployment-dependent).
- Config layering:
  - Present: env + dotenv + pydantic validation.
  - Potential gap: very large unified settings object across Stage3/4/7 increases misconfiguration surface.
- Tests:
  - Present: broad test suite including WS/REST/CLI and chaos folders.
  - Potential gap: limited explicit contract tests validating package import DAG / cycle regression and runtime mode wiring consistency.
