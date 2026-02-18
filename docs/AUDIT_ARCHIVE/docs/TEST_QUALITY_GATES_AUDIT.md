# Test Coverage & Quality Gates Audit

## Current test state

### Test inventory and scope

- The repository currently has a broad pytest suite (`tests/test_*.py`) with **80 test modules** spanning:
  - adapters (`btcturk_http`, auth, ws client, retry/rate-limit, rest client),
  - domain models/ledger/accounting,
  - stage3/4/7 services and runners,
  - replay/backtest/parity paths,
  - CLI and config behavior.
- There are clear integration-style tests mixed into the same pytest run (examples: `test_stage4_cycle_runner`, `test_stage7_run_integration`, `test_ledger_service_integration`, replay/parity/backtest tests).

### How tests are run today

- CI (`.github/workflows/ci.yml`) runs:
  1. `python scripts/guard_multiline.py`
  2. `ruff format --check .`
  3. `ruff check .`
  4. `python -m compileall src tests`
  5. `python -m mypy src/btcbot --ignore-missing-imports`
  6. `docker build -t btcbot:ci .`
  7. `python -m pytest -q`
- Local `make check` runs compileall + ruff format/lint + pytest + guard script.
- Pytest configuration in `pyproject.toml` uses quiet mode and `src` on `pythonpath`.

### Strengths

- Strong stage-aware coverage across strategy/risk/execution/OMS and adapter reliability.
- Determinism-oriented tests already exist (`test_backtest_replay_determinism`, parity tests).
- Risk/reliability paths are explicitly tested (idempotency, retries, rate limits, crash recovery).

### Gaps in current gate shape

- No separation of **unit vs integration** markers in CI; everything runs as one bucket.
- No coverage threshold gate (`coverage.py` / `pytest-cov`) in CI.
- Mypy gate is permissive (`--ignore-missing-imports`) and currently lacks module strictness tiers.
- No pre-commit config to enforce local parity with CI before commit.

## Backtesting/simulation framework status

A framework **already exists** (so no need to invent one from scratch):

- `MarketDataReplay` (CSV dataset loader + deterministic clock stepping + seed-aware replay behavior).
- `Stage7BacktestRunner` + `Stage7SingleCycleDriver` for deterministic cycle runs into sqlite outputs.
- Parity/fingerprint tooling for run comparison.

### Recommendation for framework hardening

- Keep Stage7 replay as the canonical simulation harness.
- Add an explicit “scenario DSL” layer for golden scenarios (e.g., JSON fixtures defining candles/orderbook/fills/anomalies/expected decisions).
- Add fixture versioning + schema checks for replay datasets to catch silent data drift.

## Recommended toolchain + config files

### Keep (already good)

- `ruff` for format/lint, `pytest` for tests, `mypy` for static typing, compile checks, and guard script.

### Add/adjust

1. **Pre-commit hooks** (`.pre-commit-config.yaml`)
   - Run `ruff format --check`, `ruff check`, lightweight mypy subset, and a fast pytest subset before commits.

2. **Coverage gate** (`pytest-cov`)
   - Add CI stage with threshold (e.g. start at 80%, ratchet upward).
   - Prefer per-package thresholds for critical modules (`services/execution*`, `risk*`, `adapters/btcturk*`).

3. **Mypy tiered strictness** (`mypy.ini` or `[tool.mypy]`)
   - Keep global practical defaults, but set stricter rules on critical modules:
     - `disallow_untyped_defs = True`
     - `no_implicit_optional = True`
     - `warn_return_any = True`
   - Apply first to `btcbot/services/execution*`, `btcbot/services/risk*`, `btcbot/adapters/btcturk*`.

4. **Pytest markers in `pyproject.toml`**
   - Introduce markers to split runtime:
     - `unit`
     - `integration`
     - `replay`
     - `slow`
   - CI matrix: fast unit gate on every push, full integration/replay on PR + nightly.

5. **Optional property-based tests** (`hypothesis`) for invariants
   - Good targets: quantization invariants, idempotency key stability, order-state transition legality.

### Suggested config snippets (minimal)

- `.pre-commit-config.yaml` (proposed):
  - ruff-format, ruff, mypy (critical modules), pytest unit quick subset.
- `pyproject.toml` additions:
  - pytest markers registration,
  - stricter mypy sections for critical packages,
  - optional coverage fail-under in `tool.pytest.ini_options` addopts.

## Golden path integration test plan (minimal)

### Objective

Validate deterministic end-to-end decision-to-execution behavior with a mocked exchange and fixed data timeline.

### Plan

1. **Fixture setup**
   - Deterministic clock + fixed seed.
   - Mock exchange adapter with:
     - stable `get_exchange_info`, `get_orderbook`, `get_balances`,
     - deterministic order submit/cancel responses,
     - deterministic fills stream.

2. **Cycle 1: bootstrap**
   - Inputs: no positions, sufficient TRY, valid spreads.
   - Expectation:
     - strategy emits BUY intent(s),
     - risk accepts within caps,
     - execution writes order(s) and idempotency metadata.

3. **Cycle 2: partial fill + risk budget update**
   - Inject partial fill for one order.
   - Expectation:
     - accounting/ledger update positions and pnl consistently,
     - subsequent decision sizing respects updated cash/position and cycle caps.

4. **Cycle 3: adverse condition**
   - Inject stale market data or spread spike.
   - Expectation:
     - risk mode degrades (`REDUCE_RISK_ONLY` or `OBSERVE_ONLY`),
     - no forbidden BUY writes in degrade mode.

5. **Cycle 4: idempotency/retry behavior**
   - Inject transient submit timeout then reconciliation-visible order.
   - Expectation:
     - no duplicate order placement,
     - order state resolves to single canonical order.

6. **Assertions across cycles**
   - deterministic DB snapshots/fingerprint,
   - invariant checks:
     - cash reserve floor respected,
     - no position cap breach,
     - allowed order-state transitions only,
     - replay run A/B parity fingerprint matches.

## High-priority missing tests (live-trading critical)

1. **Cross-stage policy consistency tests**
   - Ensure Stage3/4/7 risk modes and execution gates produce equivalent “block live writes” behavior for the same hazard inputs.

2. **Clock-skew and nonce monotonicity under concurrency**
   - Stress tests for auth stamp generation and server-time sync drift around boundary conditions.

3. **Exchange metadata degradation tests**
   - Missing/invalid tick/step/min-notional metadata should degrade safely and never create invalid live submits.

4. **Crash-consistency tests around submit ACK persistence window**
   - Simulate crash between exchange ACK and DB write; verify startup reconciliation recovers to one canonical order.

5. **Portfolio/risk invariants under extreme fills**
   - Large slippage, out-of-order fills, duplicate fills, and partial-fill storms should preserve ledger/accounting invariants.

6. **Config mutation and migration safety tests**
   - Ensure new env knobs preserve deterministic defaults and do not silently alter live safety semantics.

7. **E2E “kill-switch hard guarantee” tests**
   - Regardless of strategy/risk output, kill-switch must guarantee no submit/cancel side effects.

8. **Reproducibility contract tests**
   - Same seed + same dataset + same config must produce identical cycle traces, event IDs, and fingerprints across multiple runs/hosts.
