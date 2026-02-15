# Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m btcbot.cli run --dry-run
python -m pytest -q
ruff check . && ruff format --check .
```

---

## Inventory

### A1) Directory inventory (top-level, then `src/` and `tests/`)

Top-level (high signal):

- `.github/workflows/` (CI pipeline)
- `src/btcbot/` (application package)
- `tests/` (unit/integration tests)
- `docs/` (stages, runbook, architecture)
- `data/replay/` (replay dataset artifacts)
- `scripts/` (dev + guard + fixture capture)
- `.env.example`, `pyproject.toml`, `Makefile`, `README.md`

`src/btcbot/` major modules:

- `cli.py`, `config.py`
- `domain/` (models, risk, ledger, stage4/stage7 contracts)
- `services/` (cycle runners, OMS, risk, strategy, state store)
- `adapters/` (BTCTurk HTTP/auth + exchange abstractions)
- `accounting/` (stage3 accounting)
- `strategies/` (strategy interfaces + concrete strategies)
- `replay/` (dataset tooling + validation)

`tests/` major coverage areas:

- stage3 lifecycle/accounting/execution (`test_execution_service.py`, `test_accounting_stage3.py`)
- stage4 cycle and services (`test_stage4_cycle_runner.py`, `test_stage4_services.py`)
- stage7 replay/risk/metrics/oms (`test_stage7_*.py`, `test_oms_*.py`)
- adapters/config/CLI (`test_btcturk_*.py`, `test_config*.py`, `test_cli.py`)

### A2) Entrypoints

- Python package entrypoint: `btcbot = "btcbot.cli:main"` in `pyproject.toml`.
- Module entrypoint: `python -m btcbot` routes to `btcbot.cli.main` via `src/btcbot/__main__.py`.
- CLI operational commands in `src/btcbot/cli.py` include:
  - stage3 loop: `run`
  - stage4 loop: `stage4-run`
  - stage7 runtime: `stage7-run`
  - backtest/parity/report/export: `stage7-backtest`, `stage7-parity`, `stage7-report`, etc.
  - diagnostics/tooling: `health`, `doctor`, `replay-init`, `replay-capture`
- Supporting scripts:
  - `scripts/guard_multiline.py` (quality guard)
  - `scripts/capture_exchangeinfo_fixture.py` (fixture capture)

### A3) Configuration points

- Central settings model: `src/btcbot/config.py::Settings` (Pydantic Settings, `.env` loading).
- Template env file: `.env.example` (safety gates, API creds, stage7 knobs, strategy knobs).
- Config files:
  - `pyproject.toml` (deps, scripts, pytest, ruff)
  - `Makefile` (`make check` quality gate)
  - `.github/workflows/ci.yml` (CI checks)
- Secrets and sensitive runtime knobs:
  - `BTCTURK_API_KEY`, `BTCTURK_API_SECRET`
  - live-gate flags (`DRY_RUN`, `KILL_SWITCH`, `LIVE_TRADING`, `LIVE_TRADING_ACK`)

### A4) External integrations

- Exchange API: BTCTurk public/private HTTP via `src/btcbot/adapters/btcturk_http.py`.
- Persistence: SQLite (`sqlite3`) in `src/btcbot/services/state_store.py`.
- Replay data integration: local dataset files (candles/orderbook/ticker) via `src/btcbot/services/market_data_replay.py` and `src/btcbot/replay/*`.
- No message broker discovered (assumption based on repository grep + dependencies).

Examples (Inventory):
1. `btcbot.cli.main()` wires all command entrypoints and cycle runners.
2. `Settings` in `config.py` loads and validates env-driven runtime behavior.
3. `BtcturkHttpClient` in adapters provides authenticated and public API operations.

---

## Architecture

### B5) Architecture style (with evidence)

Predominantly layered + DDD-inspired modular style:

- **Domain layer**: typed models/enums/value objects in `src/btcbot/domain/*`.
- **Application/services layer**: orchestration and use-case flows in `src/btcbot/services/*` and `src/btcbot/accounting/*`.
- **Infrastructure/adapters layer**: exchange HTTP/auth + persistence (`adapters/`, `state_store.py`).
- **Interface layer**: CLI command adapter in `src/btcbot/cli.py`.

Evidence patterns:
- Services depend on domain contracts and adapters, not vice versa.
- `StateStore` encapsulates schema + persistence API used by higher services.
- `ExchangeClient` abstractions decouple runtime from BTCTurk implementation.

### B6) Layer map

- **Interface**
  - `src/btcbot/cli.py`
- **Application/Orchestration**
  - `services/stage4_cycle_runner.py`
  - `services/stage7_cycle_runner.py`
  - `services/decision_pipeline_service.py`
  - `services/execution_service.py`, `services/oms_service.py`
- **Domain**
  - `domain/models.py`, `domain/ledger.py`, `domain/risk_budget.py`, `domain/stage4.py`, `domain/order_state.py`
- **Infrastructure**
  - `adapters/btcturk_http.py`, `adapters/btcturk_auth.py`, `adapters/replay_exchange.py`
  - `services/state_store.py` (SQLite storage abstraction)

### B7) Stage concept and data flow

- **Stage 3 default loop**: market + portfolio + reconcile/fills -> accounting -> strategy -> risk -> execution (README pipeline).
- **Stage 4**: hardened lifecycle/risk/accounting integration, controlled live trading gates, cursor-based fill ingestion.
- **Stage 5/6**: strategy hardening + risk budget/degrade/anomaly + metrics atomicity (documented in `docs/STAGES.md`, stage6 docs).
- **Stage 7**: deterministic replay/backtest, dry-run OMS, risk budget v2, adaptation, parity checks.

Flow between stages:
- Stage7 runner calls/reuses Stage4 cycle machinery for baseline cycle behavior before Stage7-specific simulation/metrics layers.
- StateStore persists cross-stage artifacts (`stage4_*`, `stage7_*`, ledger tables), enabling continuity and diagnostics.

Examples (Architecture):
1. `Stage4CycleRunner.run_one_cycle()` composes rules/accounting/risk/execution services.
2. `Stage7CycleRunner.run_one_cycle()` executes Stage4 path, then Stage7 risk/universe/OMS/metrics.
3. `build_exchange_stage3/build_exchange_stage4()` in `exchange_factory.py` selects dry-run vs live adapter implementations.

---

## Data/Persistence

### C8) Storage tech and schema/migrations

- Storage: SQLite only (`sqlite3`) via `StateStore`.
- Schema management: code-first lazy migrations in `StateStore._init_db()` + `_ensure_*_schema()` methods.
- Migration style: `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` guards, no external migration tool (Alembic not present).

### C9) Core entities / VOs / events

- Domain entities/VOs:
  - Orders/intents: `Order`, `OrderIntent`, `Intent`, `Stage7Order`, `OrderEvent`
  - Accounting: `Position`, `TradeFill`, `PnLSnapshot`
  - Ledger: `LedgerEvent`, `LedgerState`, `LedgerSnapshot`
  - Risk: `RiskDecision`, risk modes and limits (`domain/risk_budget.py`, `services/risk_policy.py`)
- Relationships:
  - `OrderIntent` -> OMS -> `Stage7Order` + `stage7_order_events`
  - fills -> ledger events -> PnL snapshots/metrics
  - risk decisions constrain lifecycle actions and portfolio outputs

### C10) Important tables and read/write behavior

- Core stage3/4 tables: `orders`, `fills`, `positions`, `actions`, `intents`, `stage4_orders`, `stage4_fills`, `pnl_snapshots`, `cycle_audit`, `cursors`.
- Stage7 tables: `stage7_cycle_trace`, `stage7_ledger_metrics`, `stage7_run_metrics`, `stage7_order_intents`, `stage7_orders`, `stage7_order_events`, `stage7_idempotency_keys`, `stage7_risk_decisions`, `stage7_param_changes`, `stage7_params_checkpoints`.
- Ledger/risk/anomaly tables: `ledger_events`, `risk_decisions`, `risk_state_current`, `anomaly_events`, `degrade_state_current`.

### C11) Idempotency, dedupe, reruns

- Unique keys:
  - `actions(dedupe_key)` unique partial index.
  - stage4 fills keyed by `fill_id`.
  - stage7 event IDs deterministic and unique.
  - `stage7_idempotency_keys(key, payload_hash)` for action-level dedupe/conflict detection.
- Determinism strategy:
  - deterministic IDs/hashes for orders/events/fills in stage7.
  - replay/backtest parity and fingerprinting support in `services/parity.py` + CLI commands.
- Transaction strategy:
  - explicit SQLite transactions (`BEGIN IMMEDIATE`) and atomic save paths for stage7 cycle persistence.

Examples (Data/Persistence):
1. `StateStore.transaction()` provides explicit write transaction boundaries.
2. `StateStore._ensure_stage7_schema()` defines stage7 persistence contracts and backward-compatible column adds.
3. `OMSService.process_intents()` checks existing orders/events and idempotency keys before emitting transitions.

---

## Runtime Flows

### D12-D13) Critical sequence diagrams (text)

#### 1) Startup / bootstrap

Trigger: CLI command invocation (`manual`).

Sequence:
1. User -> `btcbot.cli.main()` parse args.
2. `Settings()` loads env/config.
3. `setup_logging()` configures logging.
4. command router dispatches to `run_cycle` / `run_cycle_stage4` / `run_cycle_stage7`.

I/O:
- Input: CLI args + env.
- Output: return code + logs + optional stdout report.

Errors/retries:
- loop wrapper retries failed cycle up to 3 attempts with exponential backoff.

Metrics/logging/state:
- structured logs (`arm_check`, loop start/stop/retry).
- no persistent state written until command-specific runner starts.

#### 2) Market data ingest

Trigger: per-cycle pull (`timer` in loop mode).

Sequence (stage3/stage4 blend):
1. `MarketDataService.get_best_bids/get_best_bid_ask` -> exchange orderbook API.
2. `ExchangeRulesService` may load/normalize exchange metadata.
3. Stage4 fill polling reads exchange fills, maps by symbol cursor.
4. `StateStore.set_cursor(...)` updates per-symbol fill cursor after successful ingest.

I/O:
- Input: symbols + exchange API snapshots.
- Output: mark prices + rules + fill deltas.

Errors/retries:
- HTTP client retry policy (`429/5xx/timeout`) in adapter.
- per-symbol fetch failures logged and degraded, not hard crash in several paths.

Metrics/logging/state:
- cursor diagnostics logs, fetch warnings.
- state in SQLite (`cursors`, stage4 fills/ledger).

#### 3) Strategy decision loop

Trigger: each cycle after data/accounting refresh (`timer`/manual).

Sequence:
1. accounting refresh computes positions/PnL context.
2. `StrategyService.generate` builds `StrategyContext`.
3. strategy (`ProfitAwareStrategyV1` or stage7 portfolio policy pipeline) emits intents/actions.
4. `RiskService`/risk policies filter and annotate decisions.

I/O:
- Input: balances, positions, orderbooks, open-order counts.
- Output: approved intents/lifecycle actions.

Errors/retries:
- validation + skip semantics for invalid metadata/constraints.
- restrictive modes (`OBSERVE_ONLY`, `REDUCE_RISK_ONLY`) enforce safe behavior.

Metrics/logging/state:
- decision summaries in cycle logs and stage7 trace tables.
- stage7 stores full per-cycle trace JSON.

#### 4) Order execution + reconciliation

Trigger: approved intents (`event from strategy/risk output`).

Sequence:
1. `ExecutionService.execute_intents` or `OMSService.process_intents` processes intents.
2. kill-switch/live-arm checks gate side effects.
3. dedupe/idempotency checks via `StateStore`.
4. submit/cancel/retry flow -> status transitions/events.
5. reconciliation updates local order status using open/all orders snapshots.

I/O:
- Input: intents + existing order state + exchange responses.
- Output: placed/simulated orders + event log updates.

Errors/retries:
- stage3: uncertain submit/cancel reconciliation attempts.
- stage7 OMS: deterministic retry-with-backoff for transient errors.

Metrics/logging/state:
- action metadata, order statuses, event history.
- tables: `actions`, `orders`, `stage7_orders`, `stage7_order_events`.

#### 5) Ledger/accounting + metrics reporting

Trigger: post-fill ingest and post-cycle closure (`event` inside cycle).

Sequence:
1. `LedgerService.ingest_exchange_updates` converts fills -> ledger events.
2. `apply_events` updates ledger state; PnL breakdown computed.
3. stage7 dry-run may simulate fills, append events, then snapshot metrics.
4. metrics persisted (`stage7_ledger_metrics`, `stage7_run_metrics`) and surfaced via CLI report/export.

I/O:
- Input: fills/events/mark prices/cash.
- Output: realized/unrealized/gross/net/equity/drawdown metrics.

Errors/retries:
- idempotent append ignores duplicate event IDs.
- transaction rollback on persistence failures.

Metrics/logging/state:
- rich financial metrics persisted in SQLite.
- report commands read and print/export these rows.

Examples (Runtime Flows):
1. `run_with_optional_loop()` provides resilient cycle scheduling and retry behavior.
2. `ExecutionService.cancel_stale_orders()` combines TTL, gating, dedupe, and reconciliation.
3. `LedgerService.financial_breakdown()` centralizes accounting metric calculations.

---

## Risk/Security

### E14) Risk controls discovered

- Safety gates: `DRY_RUN`, `KILL_SWITCH`, `LIVE_TRADING`, `LIVE_TRADING_ACK`.
- Order/position controls:
  - `MAX_ORDERS_PER_CYCLE`, `MAX_OPEN_ORDERS_PER_SYMBOL`
  - `MAX_OPEN_ORDERS`, `MAX_POSITION_NOTIONAL_TRY`
  - `NOTIONAL_CAP_TRY_PER_CYCLE`, `MIN_ORDER_NOTIONAL_TRY`
- Loss/drawdown controls:
  - stage4 risk policy (`max_daily_loss_try`, `max_drawdown_pct`, min-profit threshold)
  - stage7 risk budget (`STAGE7_MAX_DRAWDOWN_PCT`, `STAGE7_MAX_DAILY_LOSS_TRY`, cooldown logic)
- Rate limits and retries:
  - HTTP retry/backoff for API calls.
  - Stage7 OMS token-bucket throttling + retry budgets.

### E15) Security concerns

- Strengths:
  - API creds use `SecretStr` in settings.
  - request sanitization masks sensitive headers/fields in error/log contexts.
- Concerns:
  - `.env` model means secrets likely operator-managed; no vault/KMS integration.
  - potential risk if exception payloads include unsanitized third-party data (needs periodic audit).
  - local SQLite DB may contain sensitive trading telemetry; file permissions policy not enforced in code.

### E16) Concurrency hazards

- SQLite single-writer constraints mitigated with `WAL`, `busy_timeout`, and explicit transactions.
- Potential hazards:
  - parallel bot instances writing same DB can still contend/lock despite retries.
  - partial external failures (submit succeeded but acknowledgment uncertain) rely on reconciliation; timing windows remain.
  - loop mode retries can repeat upstream calls; dedupe paths critical to correctness.

### E17) Financial correctness risks

- Precision/rounding:
  - Decimal usage is strong in stage4/stage7, but some stage3 paths still use floats.
- Exchange precision/limits:
  - quantization and rules checks exist; missing metadata fallback policy must remain conservative.
- Fee/slippage modeling:
  - stage4 limitation: non-TRY fee conversion incomplete in minimal mode.
- Time handling:
  - UTC conversions are explicit in many domains; mixed/external timestamps still need careful normalization.

Examples (Risk/Security):
1. `validate_live_side_effects_policy()` blocks side effects unless armed.
2. `RiskPolicy.filter_actions()` enforces drawdown/loss/order-count/position/min-profit checks.
3. `TokenBucketRateLimiter` (used by OMSService) enforces throughput guardrails.

---

## Testing/CI

### F18) Test strategy summary

- Primarily unit + service-level integration tests in `tests/`.
- Strong coverage for:
  - adapters/auth/http parsing
  - stage3 execution/risk/accounting
  - stage4 cycle services
  - stage7 replay/parity/OMS/idempotency/risk/metrics
- Fixtures:
  - `tests/fixtures/btcturk_exchangeinfo_*.json`
- Gaps (assumption):
  - limited true live-endpoint integration tests (appropriate for safety), and no explicit property-based tests for financial invariants.

### F19) CI pipeline summary

GitHub Actions job (`.github/workflows/ci.yml`) executes:
1. install `.[dev]`
2. `python scripts/guard_multiline.py`
3. `ruff format --check .`
4. `ruff check .`
5. `python -m compileall src tests`
6. `python -m pytest -q`

Common failure points:
- style drift (ruff format/lint)
- schema/contract regressions surfaced by stage7 and oms tests
- multiline guard violations

### F20) Local run-all commands

- `make check`
- or individually:
  - `python -m compileall -q src tests`
  - `ruff format --check .`
  - `ruff check .`
  - `python -m pytest -q`

Examples (Testing/CI):
1. `tests/test_oms_idempotency.py` validates stage7 dedupe semantics.
2. `tests/test_stage7_run_integration.py` checks end-to-end stage7 cycle behavior.
3. `tests/test_execution_reconcile.py` validates uncertain execution reconciliation paths.

---

## Improvements

### G21) Prioritized plan

#### P0 (immediate safety/correctness)
1. **Eliminate mixed float/Decimal in stage3 boundary paths**
   - Risk: rounding/precision drift.
   - Effort: M.
   - Impact: `services/execution_service.py`, `services/market_data_service.py`, `domain/models.py`.
   - Acceptance: deterministic quantized outputs; no float arithmetic in order notional critical path.

2. **DB lock resilience and single-instance guard**
   - Risk: multi-process lock contention/partial failures.
   - Effort: M.
   - Impact: `services/state_store.py`, CLI startup/doctor checks.
   - Acceptance: explicit instance lock, clearer operator error on contention, soak test with concurrent runners.

3. **Secret/logging hardening pass**
   - Risk: accidental sensitive data leakage.
   - Effort: S-M.
   - Impact: adapters + logging utilities.
   - Acceptance: structured redaction tests for all error paths with request payloads.

#### P1 (reliability/observability)
4. **Traceability standardization** (`cycle_id`, `run_id`, `request_id` propagation)
   - Risk: difficult incident debugging.
   - Effort: M.
   - Impact: CLI, adapters, services.
   - Acceptance: every log in critical path includes correlation IDs.

5. **Formal invariants test suite for ledger math**
   - Risk: financial correctness regressions.
   - Effort: M.
   - Impact: `domain/ledger.py`, `services/ledger_service.py`, tests.
   - Acceptance: invariant tests for conservation, no negative lots, fee accounting consistency.

#### P2 (design evolution)
6. **Port-and-adapter boundaries for persistence**
   - Risk: tight coupling of services to SQLite-specific state store.
   - Effort: L.
   - Impact: service constructors and repository abstractions.
   - Acceptance: interfaces for order/ledger/risk repositories; SQLite adapter behind interfaces.

### G22) Code smells flagged

- `cli.py` is broad (“god interface”): command parsing + orchestration + output formatting.
- `state_store.py` is very large and multi-responsibility (schema, migration, repository APIs, metrics).
- Stage overlap can be confusing (`run`, `stage4-run`, `stage7-run` plus mixed reused services) without explicit lifecycle diagram.

### G23) Proposed target architecture (text)

Interface:
- CLI/API layer with thin command handlers

Application:
- `CycleCoordinator` (stage3/4) and `Stage7Coordinator`
- dedicated use-case services: MarketIngest, DecisionEngine, ExecutionManager, AccountingManager, MetricsPublisher

Domain:
- immutable domain models + policies + invariants

Ports:
- ExchangePort, MarketDataPort, OrderRepo, LedgerRepo, MetricsRepo, RiskStateRepo

Adapters:
- BTCTurk HTTP adapter
- Replay adapter
- SQLite repositories
- Future: Postgres adapter

Cross-cutting:
- logging/trace context, config validation, feature gates, resilience policies

Examples (Improvements):
1. Split `cli.py` into command modules (`commands/stage3.py`, `commands/stage4.py`, `commands/stage7.py`).
2. Split `state_store.py` by bounded context (orders, ledger, stage7 metrics, risk/anomalies).
3. Introduce repository interfaces consumed by runners/services to reduce infrastructural coupling.

---

## Glossary

- **Stage3**: Default runtime pipeline (market/accounting/strategy/risk/execution).
- **Stage4**: Controlled live lifecycle with stricter accounting/risk/reconcile behavior.
- **Stage5/6**: Strategy hardening + risk-budget/degrade/anomaly/metrics atomicity improvements.
- **Stage7**: Deterministic dry-run replay/backtest + OMS + risk v2 + adaptation/parity tooling.
- **Ledger**: Event-sourced financial record (`ledger_events`) used for realized/unrealized/net metrics.
- **Lifecycle actions**: Submit/cancel/replace-like actions before execution.
- **OMS**: Order management state machine for Stage7 intents/events.
- **Idempotency key**: Stable key preventing duplicate side effects during retries/reruns.
- **Parity**: Fingerprint-based reproducibility check across backtest runs.
