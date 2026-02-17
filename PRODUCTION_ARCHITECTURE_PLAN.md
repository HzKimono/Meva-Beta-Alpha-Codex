# Production-Grade Architecture Plan (Evidence-Based, Minimal Additions)

## A) Architecture scorecard

### Separation of concerns (data / strategy / risk / execution / persistence)
- **Strengths**
  - Clear package-level layering exists: `adapters/` (exchange IO), `services/` (orchestration), `strategies/`, `risk/`, `accounting/`, `domain/`, and `state_store` persistence.
  - Stage evolution is explicit (`run`, `stage4-run`, `stage7-run`) with dedicated cycle runners.
- **Gaps**
  - Runtime orchestration is concentrated in large coordinator modules (`cli.py`, `stage4_cycle_runner.py`, `stage7_cycle_runner.py`), increasing coupling and review complexity.
  - Parallel risk/execution policy paths (stage3/stage4/stage7) are valid but currently fragmented for operators.
- **Score**: **3.5 / 5**

### Testability (unit / integration / e2e, determinism)
- **Strengths**
  - Broad test surface across adapters/services/risk/accounting/stage4/stage7, plus chaos/soak suites.
  - Replay + parity tooling and deterministic backtest path exist for reproducibility.
- **Gaps**
  - End-to-end acceptance criteria are spread across many tests and docs; operational "golden-path" acceptance set is not centralized as a single production gate.
  - Some reliability behaviors (reconcile edge windows, unknown-order escalation) need tighter deterministic scenario matrices.
- **Score**: **4.0 / 5**

### Observability (structured logging / metrics / tracing)
- **Strengths**
  - Structured logging with contextual fields, arm-check and side-effect-state banners.
  - Metrics/tracing hooks exist (OpenTelemetry, Prometheus/OTLP options, cycle/reconcile/submit timings, anomaly/risk metrics persistence).
- **Gaps**
  - Stage3/Stage4/Stage7 signal naming and dashboard-oriented mapping are not yet normalized into a single operator contract.
  - Alert thresholds exist in code/config but can be made more explicit in one production profile.
- **Score**: **3.5 / 5**

### Resilience (reconnect / replay / backpressure / graceful degradation)
- **Strengths**
  - Retry/backoff, rate-limit controls, idempotency, uncertain-order reconciliation, startup recovery, and anomaly/degrade controls are present.
  - Async WS client includes queue and reconnect/backoff logic; replay dataset path supports deterministic fallback analysis.
- **Gaps**
  - Stage3 run path is REST-polling centric; operational failover between ingestion modes is not unified under one runtime policy surface.
  - Single-instance lock is explicit for Stage4/7 commands; Stage3 operational contention controls should be aligned.
- **Score**: **3.5 / 5**

---

## B) Target module boundaries and interfaces (text)

> Target keeps existing components and refines contracts; no new platform components are introduced.

1. **Runtime Shell (CLI + bootstrap)**
   - Keep `cli.py` as command entrypoint only.
   - Interface boundary: `run_cycle(command_mode, settings, deps)` style orchestration adapters (already partially present via stage runners).
   - Responsibility: settings load, policy gate enforcement, loop scheduling, process lifecycle.

2. **Exchange Gateway Boundary (`adapters/` + factory)**
   - Single conceptual interface: market data reads, account reads, order submit/cancel, order/fill reconciliation reads.
   - Keep sync/async/replay implementations but formalize parity contract behavior around error taxonomy and retry semantics.

3. **Decision Layer (`strategies/` + planning/allocation services)**
   - Input contract: market snapshot + positions + balances + policy knobs.
   - Output contract: typed intents/actions only (no side effects).
   - Existing modules already map to this (`strategy_service`, `allocation_service`, `portfolio_policy_service`, `decision_pipeline_service`).

4. **Risk Control Layer (`risk/`, `risk_service`, `risk_policy`, stage7 risk budget)**
   - Input contract: intents/actions + risk state + config limits.
   - Output contract: approved/rejected decisions with machine-readable reasons.
   - Keep stage-specific policy classes but unify reason-code taxonomy and decision envelopes.

5. **Execution & Reconciliation Layer (`execution_service*`, `order_lifecycle_service`, `reconcile_service`)**
   - Input contract: approved actions/intents.
   - Output contract: committed outcomes + uncertain outcomes requiring reconciliation.
   - Must remain sole writer for exchange side effects and order-state transitions.

6. **Persistence & Accounting Layer (`state_store`, `accounting/*`, `ledger_service`)**
   - Input contract: immutable events and normalized snapshots.
   - Output contract: deterministic state reconstruction and audit records.
   - Keep SQLite-first model with strict idempotency and replayability.

7. **Observability & Safety Layer (`observability`, anomaly/metrics services, `trading_policy`)**
   - Cross-cutting boundary that receives events from all layers.
   - Contract: stable metric/log dimensions and safety-state transitions (`NORMAL/REDUCE/OBSERVE`, arm status, kill-switch state).

---

## C) Phased action plan with acceptance criteria

## Phase 1 — Correctness + Safety (must-have)

### Task 1.1: Unify safety decision envelope across Stage3/4/7
- **Work**: Standardize reason-code schema emitted by live-side-effect gate, risk filter, and execution rejection paths (without changing behavior).
- **Acceptance criteria**:
  - Unit tests assert stable reason-code set for representative blocked scenarios (dry-run, kill-switch, not armed, drawdown breach, notional cap).
  - Logs include `cycle_id`, `reason_code`, and `decision_layer` for every rejected action.
- **Risk**: Medium
- **Dependencies**: existing `trading_policy`, `risk` modules, execution metadata fields.

### Task 1.2: Stage3 singleton-run protection alignment
- **Work**: Apply the same single-instance lock policy used by Stage4/7 to Stage3 loop command profile.
- **Acceptance criteria**:
  - Integration test: second process invocation fails fast with deterministic lock error.
  - No regression in Stage3 normal run path.
- **Risk**: Medium
- **Dependencies**: `process_lock`, CLI run command path.

### Task 1.3: Reconciliation edge-case matrix hardening
- **Work**: Expand deterministic tests for uncertain submit/cancel outcomes, unknown-order probe escalation, and stale pending recovery.
- **Acceptance criteria**:
  - Scenario tests cover: timeout after submit, 429+retry with eventual visibility, unknown order escalates, stale idempotency pending resolved.
  - Persisted statuses match expected terminal states (`COMMITTED/FAILED/UNKNOWN`) and action metadata.
- **Risk**: Medium
- **Dependencies**: `execution_service`, `state_store`, exchange adapter test doubles.

### Task 1.4: Capital policy invariants as tests
- **Work**: Add explicit invariant tests for cash reserve, per-order/per-cycle caps, max position caps, and self-financing split behavior.
- **Acceptance criteria**:
  - Property-like tests (table-driven) validate no approved action violates configured limits.
  - Self-financing function tests verify positive PnL split and negative-PnL handling.
- **Risk**: Low
- **Dependencies**: `risk/policy.py`, `allocation_service.py`, `risk/budget.py`.

---

## Phase 2 — Reliability + Monitoring

### Task 2.1: Operational SLO contract per cycle
- **Work**: Define and enforce a minimal SLO set from existing metrics (reconcile lag, submit latency, retry/429 rate, stale data rate, unknown-order escalations).
- **Acceptance criteria**:
  - `doctor` or report command surfaces SLO pass/fail summary from recent cycles.
  - CI/integration checks validate metric emission keys are present and non-null in golden runs.
- **Risk**: Medium
- **Dependencies**: metrics collectors/services, state persistence for cycle metrics.

### Task 2.2: Alertability normalization across stages
- **Work**: Normalize metric names/tags and risk-mode transitions across Stage3/4/7 into one operator-facing mapping.
- **Acceptance criteria**:
  - One runbook table maps metric -> threshold -> response action.
  - Tests verify transition events are persisted/logged for NORMAL→REDUCE→OBSERVE and reverse transitions.
- **Risk**: Medium
- **Dependencies**: anomaly/risk budget services, docs/runbook.

### Task 2.3: Replay-backed failure drills
- **Work**: Add reproducible replay drills for data staleness, retry storms, and reconciliation lag using existing replay/backtest tooling.
- **Acceptance criteria**:
  - Deterministic replay scenarios produce expected anomaly/risk mode outcomes.
  - Parity fingerprints remain stable under fixed seed/time windows.
- **Risk**: Low
- **Dependencies**: `stage7_backtest_runner`, replay dataset tooling, parity checks.

---

## Phase 3 — Performance + Maintainability

### Task 3.1: Coordinator decomposition by seams (no behavior change)
- **Work**: Incrementally extract orchestration helpers from `cli.py`, `stage4_cycle_runner.py`, `stage7_cycle_runner.py` into smaller composition functions.
- **Acceptance criteria**:
  - Public CLI behavior unchanged (golden snapshot tests).
  - Reduced function/module complexity thresholds (lint/static metrics) and increased unit coverage for extracted seams.
- **Risk**: High
- **Dependencies**: comprehensive regression tests from Phase 1.

### Task 3.2: Adapter contract conformance suite
- **Work**: Define a shared conformance test suite for sync BTCTurk adapter, async rest client wrapper, and replay adapter on core operations.
- **Acceptance criteria**:
  - Same behavior for success/error/idempotency expectations across adapter implementations.
  - Contract tests run in CI with fixtures/mocks.
- **Risk**: Medium
- **Dependencies**: adapter interfaces and test fixture coverage.

### Task 3.3: Persistence API narrowing for `StateStore`
- **Work**: Introduce domain-specific repository facades over existing `StateStore` methods to reduce broad coupling.
- **Acceptance criteria**:
  - New code paths use narrowed interfaces; existing behavior unchanged.
  - Integration tests confirm schema and migration compatibility.
- **Risk**: Medium
- **Dependencies**: Stage3/4/7 services touching persistence.

### Task 3.4: Performance budget instrumentation
- **Work**: Set and monitor per-cycle CPU/latency budgets using existing metrics hooks; optimize highest-cost steps only after measurement.
- **Acceptance criteria**:
  - Baseline and post-change measurements captured for market fetch, risk filter, execution/reconcile phases.
  - No safety regression in guardrail metrics under load/soak tests.
- **Risk**: Low
- **Dependencies**: soak tests, metrics pipeline.

---

## Priority ordering rationale
1. **Phase 1 first** because control correctness and deterministic safety boundaries are prerequisites for production reliability.
2. **Phase 2 next** to make failures observable and operationally actionable.
3. **Phase 3 last** to reduce complexity and optimize only after correctness/reliability are proven.
