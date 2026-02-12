# Stage 6 Readiness Audit Report (Stage 5 + Stage 4 Integration)

## Executive summary

**Readiness verdict:** **PASS WITH CONDITIONS** — core Stage 4/5 integration is sound and P0 hardening items in this audit PR are addressed; Stage 6 can start after merging this PR while tracking P1 typing/tooling debt.  

- ✅ Strong architecture separation exists across `domain`, `services`, `adapters`, and CLI orchestration.
- ✅ Stage 5 pipeline is integrated into Stage 4 and falls back to bootstrap intents when pipeline output is empty.
- ✅ Live-trading safety gates (dry-run, kill-switch, explicit arming) are consistently applied.
- ✅ P0 addressed in this PR: exchange rules now prefer exchange-provided `tickSize`/`stepSize` with safe fallback to scale-derived values.
- ✅ P0 addressed in this PR: SQLite store now sets timeout + busy-timeout + WAL for better contention resilience.
- ⚠️ P1: No CI workflow existed in-repo; quality gates were local-only.
- ⚠️ P1: mypy readiness is currently low (34 errors) and no typed CI gate exists.

## Phase 0 — Repository inventory

### Top-level inventory
- Config / packaging: `pyproject.toml`, `.env.example`.
- Docs: `README.md`, `docs/stage4.md`, `AUDIT_REPORT.md`.
- Runtime artifact: `btcbot_state.db`.
- Source root: `src/btcbot/` with modules:
  - `domain/`, `services/`, `adapters/`, `risk/`, `strategies/`, `accounting/`, CLI files.
- Tests: `tests/` with Stage 3/4/5 coverage.
- Tooling script: `scripts/guard_multiline.py`.
- CI config after this audit PR: `.github/workflows/ci.yml`.

### Key entrypoints
- CLI entrypoint: `btcbot.cli:main` in `pyproject.toml`.
- Runtime command entrypoints: `run`, `stage4-run`, and `health` in `src/btcbot/cli.py`.
- Stage 5 orchestrator: `DecisionPipelineService.run_cycle` in `src/btcbot/services/decision_pipeline_service.py`.
- Stage 4 cycle hook: Stage 4 runner invokes decision pipeline in `src/btcbot/services/stage4_cycle_runner.py`.

---

## Phase 1 — Architectural boundary audit

## Layer responsibilities and boundary quality

### `domain/`
- Contains immutable-ish contracts and quantization/rule types (`ExchangeRules`, `Order`, `Quantizer`, intent/allocation models).
- No direct I/O observed in domain classes.
- Boundary quality: **Good**.

### `services/`
- Orchestration and policy logic:
  - Stage 4 cycle composition (`Stage4CycleRunner`).
  - Stage 5 decision pipeline (`DecisionPipelineService`).
  - Allocation/risk/execution/accounting orchestration.
- Boundary quality: **Good**, with acceptable coupling to `StateStore`.

### `adapters/`
- Exchange protocol and HTTP boundary (`btcturk_http.py`, protocol interfaces, action-to-order adapter).
- Boundary quality: **Good**. Conversion/parsing mostly kept here.

### `cli`
- Thin orchestration wrapper; no heavy business logic beyond wiring and top-level policy gate checks.
- Boundary quality: **Good**.

## Stage 5 pipeline verification
Implemented flow in code:  
`universe -> strategies -> intents -> allocation -> action_to_order -> stage4 cycle lifecycle/risk/execution`.

- Universe: `select_universe(...)`.
- Strategy intent generation: registry in `DecisionPipelineService`.
- Allocation: `AllocationService.allocate(...)`.
- Mapping: `sized_action_to_order(...)`.
- Stage 4 integration: Stage 4 runner uses pipeline order requests before bootstrap fallback.

## Stage 4 pipeline verification
Implemented flow in code:  
`reconcile(open orders) -> fills/accounting -> lifecycle planning -> risk policy -> execution -> state/audit persistence`.

- Reconcile: `ReconcileService.resolve(...)`.
- Risk gate: `RiskPolicy.filter_actions(...)`.
- Execution side effects: `ExecutionService.execute_with_report(...)`.
- Persistence/audit: `StateStore.record_cycle_audit(...)`, order/position/fill tables.

---

## Phase 2 — Correctness & safety audit

## 1) Decimal usage
- Most Stage 4/5 money and quantity calculations use `Decimal` throughout core services.
- Residual float usage remains in legacy Stage 3-facing models (`Balance.free/locked`, some orderbook parsing as float).
- Verdict: **Mostly correct for Stage 4/5 paths**, but typed legacy models still mix float/Decimal.

## 2) Symbol normalization / canonicalization
- Canonicalization is centralized (`canonical_symbol`), used in config parsing, allocation, strategy universe, and Stage 4 map keys.
- Adapter pair symbol formatter currently maps to normalized symbol only; explicit venue-specific formatter abstraction is absent.
- Verdict: **Acceptable**, but should be hardened before multi-venue support.

## 3) PairInfo -> ExchangeRules correctness
- Mapping now prefers exchange-provided `tickSize`/`stepSize` and falls back to precision-derived quantization when explicit values are unavailable.
- Added regression coverage for tick/step precedence and quantization outcomes.
- Verdict: **Resolved in this PR**.

## 4) Quantization + min-notional
- Quantization and `validate_min_notional` are applied in Stage 5 mapping and bootstrap intent builder.
- Execution layer re-validates min notional before submission, adding defense-in-depth.
- Verdict: **Strong**.

## 5) Safety switches
- Kill-switch blocks writes in execution service.
- Dry-run/live gating requires full arming (`LIVE_TRADING`, ack, non-dry-run, kill switch off).
- Policy block writes audit record in Stage 4 CLI flow.
- Verdict: **Strong**.

## 6) Idempotency
- Deterministic client order IDs in action mapping.
- Stage 4 execution dedupes by `client_order_id_exists(...)` and terminal checks before submit/cancel.
- Verdict: **Good**.

## 7) Error handling / partial failures
- Stage 4 handles symbol-local failures for open-order and fills fetch, continues cycle, and records counts.
- Adapter retries GET/public and private GET requests with bounded backoff.
- Private write operations (submit/cancel) intentionally do not retry, relying on reconcile.
- Verdict: **Good with operational caveat** (needs runbook for uncertain outcomes).

## 8) State persistence / schema / migrations
- Schema self-heals via `CREATE TABLE IF NOT EXISTS` + additive column checks.
- No external migration framework/version history beyond lightweight schema table.
- Before this PR there was no explicit busy timeout/WAL tuning.
- Verdict: **Improved in this PR**, still lacking formal migration tooling.

---

## Phase 3 — Tests, CI, tooling audit

## Current tests
- Unit/functional coverage is broad for config parsing, adapters, strategy/allocation, Stage 4 services, cycle runner, and persistence.
- Deterministic tests exist for quantization and min-notional rejection paths.

## Missing/high-risk tests
- Added in this PR: direct tests for `PairInfo.tickSize/stepSize` precedence over scale mapping.
- Multi-process SQLite contention behavior lacks stress tests.
- More edge cases desirable for extreme precision and tiny balances on Stage 5 mapped orders.

## CI/tooling
- Before this PR, repository did not contain `.github/workflows/*`.
- This PR adds a minimal CI workflow running format-check, lint, and pytest on Python 3.12.
- mypy is not configured as a required gate and currently fails with many issues.

## Packaging/dependency posture
- `pyproject.toml` is structurally valid and defines CLI script.
- Dependencies are version-ranged (not lockfile-pinned).
- Security/reproducibility could improve with pinned lock workflow.

---

## Phase 4 — Performance & maintainability audit

- Hot path orchestration is straightforward and mostly linear per symbol.
- Logging is structured and audit-rich in Stage 4 runner.
- `StateStore` has grown large; partitioning read/write concerns could improve maintainability later.
- Type coverage is partial; mypy debt is substantial.

---

## Prioritized fix list

## P0 (blockers before Stage 6)
- None remaining in this audit branch after hardening changes in this PR.

## P1 (high priority)
1. **CI absent in-repo**
   - **Files:** `.github/workflows/ci.yml` (added in this PR).
   - **Risk:** Regressions not automatically caught on push/PR.
   - **Fix:** Enforce lint + tests in GitHub Actions.
   - **Blocker:** No, but high priority.

2. **Typing readiness (mypy debt)**
   - **Files:** multiple (`config.py`, `state_store.py`, `btcturk_http.py`, `allocation_service.py`, etc.).
   - **Risk:** Hidden interface/type bugs in Stage 6 changes.
   - **Fix:** Introduce staged mypy cleanup and then CI gate.
   - **Blocker:** No, but high priority.

## P2 (medium)
- Add formal migration system (Alembic or explicit migration scripts).
- Add stronger rule/symbol invariants at adapter boundaries.
- Improve uncertain submit/cancel operational observability (explicit unresolved action states).

## P3 (nice-to-have)
- Introduce lockfile/pinned dependency workflow.
- Break `StateStore` into smaller repository modules.

---

## Audit PR scope (this PR)
This PR addresses selected P0/P1 hardening items only (no Stage 6 features):
1. Exchange rules hardening: `build_exchange_rules(...)` now honors explicit exchange `tickSize`/`stepSize` when present (with precision fallback).
2. Added regression test for tick/step precedence and quantization behavior.
3. SQLite connection hardening in `StateStore` (`timeout`, `busy_timeout`, `journal_mode=WAL`).
4. Added regression test validating SQLite operational pragmas.
5. Added baseline GitHub Actions CI workflow for `ruff format --check`, `ruff check`, and `pytest -q`.
