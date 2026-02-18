# PROJECT_REPORT

## 1) Executive Summary

**What Meva is**
- Meva is a staged BTCTurk spot trading system with a Python CLI front door (`btcbot.cli.main`) and modular runtime layers for market data, strategy, risk, execution, and persistence.
- The codebase supports Stage 3/4 operational cycles and Stage 7 dry-run analytics/replay workflows through one command surface in `src/btcbot/cli.py`.

**Stage 7 objective (plain language)**
- Stage 7 is a **dry-run-only confidence layer**: produce deterministic cycle traces, risk decisions, simulated OMS outcomes, and ledger metrics before any live-trading expansion.
- Core Stage 7 orchestration happens in `Stage7CycleRunner.run_one_cycle_with_dependencies` (`src/btcbot/services/stage7_cycle_runner.py`).

**What is working today**
- `stage7-run` enforces dry-run + feature-flag and executes full cycle orchestration (`run_cycle_stage7`, `Stage7CycleRunner.run_one_cycle`).
- Backtest pipeline runs replay-driven cycles and writes SQLite outputs (`run_stage7_backtest`, `Stage7BacktestRunner.run`, `Stage7SingleCycleDriver.run`).
- Parity computes deterministic fingerprints over cycle/ledger outputs (`compute_run_fingerprint`, `compare_fingerprints`).
- Doctor command validates gate coherence, dataset readiness, DB path sanity, and symbol metadata usability (`run_health_checks`, `_check_exchange_rules`).

**What is blocked / risky now**
- Stage 7 remains intentionally non-live (`run_cycle_stage7` requires dry-run and `STAGE7_ENABLED`; `Settings.validate_stage7_safety` rejects live Stage 7).
- Metadata fragility still exists at rules boundary: `ExchangeRulesService.get_rules` raises `ValueError` when status is `missing/invalid` and metadata is required; there are call paths where this can still fail-cycle instead of degrade.
- CI does not currently run compile checks or guard script despite README quality-gate docs (`.github/workflows/ci.yml` only runs ruff+pytest).

---

## 2) Architecture Overview

### Layered model

1. **CLI Layer**
   - Entry: `btcbot.cli.main` (`src/btcbot/cli.py`), `python -m btcbot` (`src/btcbot/__main__.py`), package script (`pyproject.toml` -> `btcbot = btcbot.cli:main`).
   - Responsibility: command parsing, argument-level safety checks, command dispatch.

2. **Orchestration Layer**
   - Stage runners and command handlers:
     - `run_cycle`, `run_cycle_stage4`, `run_cycle_stage7`
     - `Stage4CycleRunner.run_one_cycle`
     - `Stage7CycleRunner.run_one_cycle[_with_dependencies]`
     - `Stage7BacktestRunner.run`, `Stage7SingleCycleDriver.run`
   - Responsibility: wire dependencies, enforce mode/gate policy, manage cycle lifecycle.

3. **Service Layer**
   - Portfolio/risk/execution/persistence services (examples):
     - `UniverseSelectionService.select_universe`
     - `PortfolioPolicyService.build_plan`
     - `OrderBuilderService.build_intents`
     - `OMSService.process_intents`
     - `LedgerService.snapshot`
     - `StateStore.save_stage7_cycle`
   - Responsibility: deterministic business workflows and side-effect boundaries.

4. **Domain Layer**
   - Typed contracts and enums in `src/btcbot/domain/*` (e.g., `Mode`, risk decision models, order intent models, lifecycle models).
   - Responsibility: stable data contracts across layers.

5. **Adapter Layer**
   - Exchange/replay adapters:
     - Stage3/BTCTurk adapters in `src/btcbot/adapters/btcturk_http.py`, `src/btcbot/adapters/exchange.py`.
     - Stage4 protocol in `src/btcbot/adapters/exchange_stage4.py`.
     - Replay adapter in `src/btcbot/adapters/replay_exchange.py`.
   - Responsibility: normalize external API payloads into internal models.

6. **External Layer**
   - BTCTurk HTTP APIs and local replay datasets (`data/replay`, `btcbot.replay.validate`, `btcbot.replay.tools`).

### Boundaries and interfaces
- **Primary execution boundary**: only execution services perform submit/cancel side effects (`ExecutionService` in Stage 4, OMS simulation in Stage 7 dry-run).
- **Persistence boundary**: `StateStore` owns SQLite schema, transactions, upserts, and dedupe semantics.
- **Rules boundary**: `ExchangeRulesService` maps exchange metadata to quantization/min-notional contracts and can return status (`ok/fallback/missing/invalid`).
- **Policy boundary**: live side-effects gate centralized in `validate_live_side_effects_policy` (`src/btcbot/services/trading_policy.py`) and configuration validator `Settings.validate_stage7_safety`.

---

## 3) End-to-End Flows (step-by-step)

### A) `stage7-run` (dry-run) — one cycle

Command path: `btcbot.cli.main` -> `run_cycle_stage7` -> `Stage7CycleRunner.run_one_cycle` -> `run_one_cycle_with_dependencies`.

1. **Preflight gating**
   - `run_cycle_stage7` checks `--dry-run` and `STAGE7_ENABLED`.
   - `Settings.validate_stage7_safety` guarantees `STAGE7_ENABLED => DRY_RUN=true and LIVE_TRADING=false`.

2. **Dependency bootstrap**
   - Loads active Stage7 params (`StateStore.get_active_stage7_params`) into runtime copy.
   - Builds services: universe selection, portfolio policy, order builder, risk budget, ledger, OMS.

3. **Measure/input collection**
   - Pull balances/mark context and previous persisted state (`StateStore.get_latest_stage7_ledger_metrics`, `get_latest_stage7_risk_decision`).
   - Build `Stage7RiskInputs` and call `Stage7RiskBudgetService.decide`.

4. **Universe selection**
   - `UniverseSelectionService.select_universe` computes ranked symbols deterministically.

5. **Planning**
   - Resolve mark prices (`Stage4CycleRunner.resolve_mark_prices`).
   - `PortfolioPolicyService.build_plan` generates actions under mode + constraints.

6. **Gating and mode combine**
   - Base mode from state (`StateStore.get_latest_risk_mode`) + Stage7 risk mode combined via `combine_modes`.
   - Metadata-invalid policy (`stage7_rules_invalid_metadata_policy`) may force `OBSERVE_ONLY`.

7. **Rules/validation + intent build**
   - Rules statuses gathered via `ExchangeRulesService.get_symbol_rules_status`.
   - `OrderBuilderService.build_intents` quantizes price/qty + validates notional (`validate_notional`), emitting PLANNED or SKIPPED intents.

8. **OMS boundary (still dry-run)**
   - If not `OBSERVE_ONLY`: `OMSService.reconcile_open_orders` + `OMSService.process_intents` using `Stage7MarketSimulator`.
   - No live exchange writes in Stage7 path.

9. **Ledger snapshot + metrics**
   - `LedgerService.snapshot` computes gross/net/fees/slippage/equity/drawdown.

10. **Atomic persistence**
    - `StateStore.save_stage7_cycle` writes cycle trace + ledger metrics (+ optional intents/risk_decision/run_metrics) in one transaction.

11. **Optional adaptation**
    - Adaptation service can evaluate/persist parameter changes when enabled.

### B) `stage7-backtest` + `stage7-parity` + `replay-init`

1. **`replay-init` guarantees** (`run_replay_init` -> `init_replay_dataset`)
   - Creates required folder structure (`candles/`, `orderbook/`, optional `ticker/`) + README/schema.
   - Optional synthetic deterministic sample generation by seed.

2. **`stage7-backtest` guarantees** (`run_stage7_backtest`)
   - Validates dataset contract (`validate_replay_dataset`) before running.
   - Runs deterministic replay cycles through `Stage7BacktestRunner.run` -> `Stage7SingleCycleDriver.run`.
   - Writes SQLite outputs and computes final run fingerprint (`compute_run_fingerprint`).

3. **`stage7-parity` guarantees** (`run_stage7_parity`)
   - Produces comparable fingerprints over canonicalized Stage7 outputs in a time window.
   - Returns success/failure by exact fingerprint equality (`compare_fingerprints`).
   - Reports missing required parity tables via `find_missing_stage7_parity_tables`.

---

## 4) State / DB

DB owner: `StateStore` (`src/btcbot/services/state_store.py`).

### Stage7 core tables (from `_ensure_stage7_schema`)

1. **`stage7_cycle_trace`**
   - PK: `cycle_id`.
   - Main columns: `ts`, `selected_universe_json`, `universe_scores_json`, `intents_summary_json`, `mode_json`, `order_decisions_json`, `portfolio_plan_json`, `order_intents_json`, `active_param_version`, `param_change_json`.
   - Index: `idx_stage7_cycle_trace_ts` on `ts`.

2. **`stage7_ledger_metrics`**
   - PK/FK: `cycle_id` -> `stage7_cycle_trace(cycle_id)`.
   - Columns: `gross_pnl_try`, `realized_pnl_try`, `unrealized_pnl_try`, `net_pnl_try`, `fees_try`, `slippage_try`, `turnover_try`, `equity_try`, `max_drawdown`, `ts`.
   - Index: `idx_stage7_ledger_metrics_ts`.

3. **`stage7_run_metrics`**
   - PK: `cycle_id`.
   - Columns include mode fields, counts, performance timings, quality/alert JSONs, run_id.
   - Indexes: `idx_stage7_run_metrics_ts`, `idx_stage7_run_metrics_run_id`.

4. **`stage7_order_intents`**
   - PK: `client_order_id`.
   - Columns: `cycle_id`, `ts`, `symbol`, `side`, `order_type`, `price_try`, `qty`, `notional_try`, `status`, `intent_json`.
   - Index: `idx_stage7_order_intents_cycle_id`.

5. **`stage7_orders`**
   - PK: `order_id`; unique key: `client_order_id`.
   - Columns: order identity, symbol/side/type, quantities, status, `intent_hash`, `last_update`.
   - Index: `idx_stage7_orders_client_order_id`.

6. **`stage7_order_events`**
   - PK: `event_id`.
   - Columns: `ts`, `cycle_id`, `order_id`, `client_order_id`, `event_type`, `payload_json`.
   - Index: `idx_stage7_order_events_client_ts`.

7. **`stage7_idempotency_keys`**
   - PK: `key`.
   - Columns: `ts`, `payload_hash`.

8. **`stage7_risk_decisions`**
   - PK: autoincrement `id`.
   - Columns: `cycle_id`, `decided_at`, `mode`, `reasons_json`, `cooldown_until`, `inputs_hash`.
   - Index: `idx_stage7_risk_decisions_decided_at`.

9. **Adaptation parameter tables**
   - `stage7_params_active` (PK `key`), `stage7_param_changes` (PK `change_id`), `stage7_params_checkpoints` (PK `version`).

### Idempotency and atomic strategy
- Transaction wrapper: `StateStore.transaction` uses `BEGIN IMMEDIATE`, commit/rollback semantics.
- Atomic cycle write: `save_stage7_cycle` performs coordinated upserts/inserts for trace + metrics + optional intents/risk/run-metrics within one transaction.
- Upsert semantics:
  - `ON CONFLICT(cycle_id)` for cycle-level tables.
  - `ON CONFLICT(client_order_id)` for intent/order records.
  - `INSERT OR IGNORE` for event dedupe (`append_stage7_order_events`).
- Deterministic identity:
  - Intent IDs generated by `OrderBuilderService._client_order_id`.
  - Backtest cycle IDs deterministic in `Stage7SingleCycleDriver.run` (`bt:YYYY...:counter`).

### What persists per cycle (Stage7)
- Always: cycle trace row, ledger metrics row.
- Usually: run metrics, order intents, risk decision row.
- Optional: adaptation metadata (`active_param_version`, `param_change_json`).

---

## 5) Guardrails / Gating

### Gate variables
- `KILL_SWITCH`, `DRY_RUN`, `LIVE_TRADING`, `LIVE_TRADING_ACK`, `STAGE7_ENABLED` in `Settings` (`src/btcbot/config.py`).

### Enforcement points
1. **Config-level invariants (`Settings.validate_stage7_safety`)**
   - Rejects invalid combinations at load time:
     - Stage7 with non-dry-run/live enabled.
     - `LIVE_TRADING=true` with `DRY_RUN=true`.
     - Missing `LIVE_TRADING_ACK=I_UNDERSTAND`.
     - `LIVE_TRADING=true` with `KILL_SWITCH=true`.
     - Missing API creds in live mode.

2. **Command-level hard stops (`run_cycle_stage7`)**
   - If not dry-run -> exit code 2.
   - If `STAGE7_ENABLED` false -> exit code 2.

3. **Execution-level live side-effect policy (`validate_live_side_effects_policy`)**
   - Returns block reasons (`KILL_SWITCH`, `DRY_RUN`, `LIVE_NOT_ARMED`).
   - `execution_service._ensure_live_side_effects_allowed` raises `LiveTradingNotArmedError` with explicit message.

4. **Stage4 execution writes (`ExecutionService.execute_with_report`)**
   - If kill switch active: immediate no-op report.
   - If live flag incoherent: runtime error.

### Failure behavior
- CLI wrappers (`run_cycle_stage4`, `run_cycle_stage7`) catch unexpected exceptions and return non-zero codes with logged context.
- For Stage7: failure during orchestration returns code 1; precondition failures return code 2.

---

## 6) Exchange Rules / Pre-trade Validation (Stage4 boundary)

### Where rules come from
- `ExchangeRulesService.get_symbol_rules_status` pulls `get_exchange_info()` and extracts metadata via `_extract_rules`.
- Field extraction supports aliases like `tickSize/stepSize/minTotalAmount` and filter structures (`PRICE_FILTER`, `LOT_SIZE`, etc.).
- Fallback knobs come from settings (`stage7_rules_fallback_*`) when metadata fallback is allowed.

### Required/validated rule elements
- `tick_size`, `lot_size`, `min_notional_try` must be > 0 (`_is_valid_rules`).
- Quantization and notional checks:
  - `quantize_price`
  - `quantize_qty`
  - `validate_notional` / `validate_min_notional`

### Stage4 pre-trade validation logic
- `ExecutionService.execute_with_report` (Stage4) calls `rules_service.get_rules`.
- On `ValueError`, it records `missing_exchange_rules` rejection and continues the cycle (reject+continue behavior already present there).
- Then quantizes and applies min-notional guard; violations become explicit rejected records.

### Explicit P0 crash path to document
- Current crash vector:
  1. Metadata is missing/invalid and `stage7_rules_require_metadata=true`.
  2. `ExchangeRulesService.get_rules(symbol)` raises `ValueError("No usable exchange rules...")`.
  3. Any caller that does not catch this exception fails the cycle.
- This can surface outside the Stage4 execution catch-path (e.g., future/new callers or direct quantize helpers that rely on `get_rules`).

### Recommended stable degrade behavior
1. **Reject+continue as default for all pre-trade callers**
   - Mirror Stage4 behavior: convert rules failures into `SKIPPED/REJECTED` order decisions, never hard-fail full cycle for single-symbol metadata issues.
2. **Centralize metadata failure policy**
   - Introduce common wrapper around `get_rules` used by Stage4/Stage7 intent+execution paths.
3. **Doctor hardening**
   - Keep `_check_exchange_rules` and extend with explicit fail/warn split by runtime mode + policy; ensure preflight can block unsafe runs early.

---

## 7) Tests & CI

### CI gates today
- `.github/workflows/ci.yml` runs:
  - `ruff format --check .`
  - `ruff check .`
  - `pytest -q`

### Test inventory by capability (examples from `tests/`)
- **CLI/config/ops**: `test_cli.py`, `test_config.py`, `test_config_symbol_parsing.py`, `test_doctor.py`, `test_env_example.py`.
- **Adapters/BTCTurk IO**: `test_btcturk_http.py`, `test_btcturk_auth.py`, `test_btcturk_exchangeinfo_parsing.py`, `test_btcturk_submit_cancel.py`.
- **Stage3/4 pipeline**: `test_execution_service.py`, `test_execution_reconcile.py`, `test_stage4_cycle_runner.py`, `test_stage4_services.py`, `test_risk_policy_stage3.py`, `test_strategy_stage3.py`.
- **State/ledger/accounting**: `test_state_store*.py`, `test_ledger_domain.py`, `test_ledger_service_integration.py`, `test_accounting_stage3.py`.
- **Stage6/Stage7 features**: `test_stage6_*.py`, `test_stage7_backtest_contracts.py`, `test_stage7_run_integration.py`, `test_stage7_report_cli.py`, `test_stage7_risk_budget_service.py`, `test_stage7_ledger_math.py`.
- **Replay/parity/data**: `test_replay_tools.py`, `test_replay_exchange.py`, `test_backtest_parity_pipeline.py`, `test_backtest_replay_determinism.py`, `test_backtest_data_gaps.py`.

### Gaps
- CI does not execute README-documented `python scripts/guard_multiline.py` and `python -m compileall src tests`.
- No explicit schema migration compatibility matrix tests (forward/backward migrations around `ALTER TABLE` branches in `StateStore`).
- No dedicated chaos tests for intermittent `get_exchange_info` failures during Stage7 intent build.

---

## 8) Backlog (P0 / P1 / P2)

### P0 (stability/safety)

1. **Rules failure should never kill full cycle**
- **Where**: `ExchangeRulesService.get_rules`, callers in Stage7/other paths.
- **Issue**: `ValueError` on missing/invalid metadata can hard-fail caller if uncaught.
- **Direction**: add safe-wrapper API returning typed status + reason, enforce reject/skip continuation semantics in all call sites.

2. **Preflight metadata enforcement for production profiles**
- **Where**: `run_health_checks._check_exchange_rules`.
- **Issue**: warning/fail logic is mode-conditional; strengthen policy mapping for Stage4 live and Stage7 metadata-required runs.
- **Direction**: introduce explicit “cannot start” status surfaced by CLI before cycle start.

3. **Transaction error observability**
- **Where**: `StateStore.save_stage7_cycle`.
- **Issue**: atomicity is strong, but failure diagnostics can be sparse.
- **Direction**: enrich exception context with cycle_id + sub-step marker for operability.

### P1 (operability/perf)

1. **CI parity with documented quality gates**
- **Where**: `.github/workflows/ci.yml`, `scripts/guard_multiline.py`.
- **Direction**: add compileall + guard script to CI for drift prevention.

2. **Schema evolution tests for Stage7 tables**
- **Where**: `StateStore._ensure_stage7_schema` and migration-style `ALTER TABLE` branches.
- **Direction**: add regression tests covering older DB snapshots upgraded in place.

3. **Backtest diagnostics export ergonomics**
- **Where**: `run_stage7_backtest_export`, `fetch_stage7_cycles_for_export`.
- **Direction**: include deterministic schema version + query window metadata in export output.

### P2 (product depth)

1. **Richer replay capture contract**
- **Where**: `capture_replay_dataset`, `validate_replay_dataset`.
- **Direction**: add capture provenance manifest (API source/version/timestamps/checksums).

2. **Policy introspection endpoint/report**
- **Where**: `trading_policy`, `doctor`, Stage7 reporting commands.
- **Direction**: expose computed gate/mode rationale as a first-class report artifact.

3. **Cross-stage observability unification**
- **Where**: Stage4 metrics + Stage7 run metrics.
- **Direction**: standardize metric names and produce shared dashboard-friendly schema.

---

## 9) Appendix — Concept → files → key classes/functions index

| Concept | Primary files | Key symbols |
|---|---|---|
| CLI/command router | `src/btcbot/cli.py`, `src/btcbot/__main__.py` | `main`, `run_cycle_stage7`, `run_stage7_backtest`, `run_stage7_parity`, `run_replay_init` |
| Settings & safety invariants | `src/btcbot/config.py` | `Settings`, `validate_stage7_safety` |
| Stage7 cycle orchestration | `src/btcbot/services/stage7_cycle_runner.py` | `Stage7CycleRunner.run_one_cycle_with_dependencies` |
| Stage7 backtest driver | `src/btcbot/services/stage7_backtest_runner.py`, `src/btcbot/services/stage7_single_cycle_driver.py` | `Stage7BacktestRunner.run`, `Stage7SingleCycleDriver.run` |
| Replay tools/contracts | `src/btcbot/replay/tools.py`, `src/btcbot/replay/validate.py` | `init_replay_dataset`, `capture_replay_dataset`, `validate_replay_dataset` |
| Parity/fingerprint | `src/btcbot/services/parity.py` | `compute_run_fingerprint`, `compare_fingerprints`, `find_missing_stage7_parity_tables` |
| Exchange/rules normalization | `src/btcbot/services/exchange_rules_service.py` | `get_symbol_rules_status`, `get_rules`, `validate_notional`, `get_rules_stage4` |
| Stage4 execution boundary | `src/btcbot/services/execution_service_stage4.py` | `ExecutionService.execute_with_report` |
| Stage7 intent build | `src/btcbot/services/order_builder_service.py` | `build_intents`, `_build_action_intent`, `_client_order_id` |
| Stage7 OMS simulation | `src/btcbot/services/oms_service.py` | `process_intents`, `reconcile_open_orders`, `Stage7MarketSimulator` |
| Persistence & transactions | `src/btcbot/services/state_store.py` | `transaction`, `_ensure_stage7_schema`, `save_stage7_cycle`, `upsert_stage7_orders`, `append_stage7_order_events` |
| Ledger metrics | `src/btcbot/services/ledger_service.py` | `LedgerService.snapshot` |
| Risk mode decision | `src/btcbot/services/stage7_risk_budget_service.py` | `Stage7RiskBudgetService.decide` |
| Health/doctor checks | `src/btcbot/services/doctor.py` | `run_health_checks`, `_check_exchange_rules` |
| CI quality gates | `.github/workflows/ci.yml` | `lint-type-test` job |
