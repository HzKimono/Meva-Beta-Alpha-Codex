# TECHNICAL AUDIT REPORT

## 1) Executive Summary (max 15 bullets)
- System purpose: a Python trading bot for BTCTurk spot markets with staged runtimes (Stage 3/4/7), safety gates, and deterministic replay/backtest support.
- End-to-end flow: CLI bootstraps settings/secrets/logging -> acquires process lock -> builds exchange/services -> fetches market/account state -> generates intents/actions -> applies risk/policy gates -> executes or simulates orders -> persists state/metrics/audit to SQLite.
- Core orchestration sits in `src/btcbot/cli.py` and service modules under `src/btcbot/services/`.
- Exchange integration is implemented primarily by `src/btcbot/adapters/btcturk_http.py` (sync client) plus async REST/WS adapters under `src/btcbot/adapters/btcturk/`.
- State is persisted in SQLite via `src/btcbot/services/state_store.py` with WAL mode and multiple schemas (orders/fills/positions/intents/ledger/risk/anomalies/stage7/audit).
- Strategy layer includes protocol abstractions and concrete implementations (`ProfitAwareStrategyV1`, Stage5 baseline mean-reversion).
- Risk controls include policy-level caps (open orders, cooldown, notional caps), live-write arming checks, kill-switch, and safe-mode.
- Security controls include secret provider abstraction, scope/rotation validation, and redaction of sensitive log fields.
- Observability includes JSON logging, OpenTelemetry hooks, and metrics for REST/WS/reconciliation/latency.
- CI includes lint/type/security/unit/integration/soak jobs and Docker build.
- Top risk #1 (financial): SQLite file `btcbot_state.db` is present in repo root; possible accidental leakage/state confusion.
- Top risk #2 (reliability): mixed Stage3/4/7 pathways and many runtime flags increase misconfiguration risk.
- Top risk #3 (security): dotenv loading and env injection are local-file based; secrets-at-rest hardening is limited.
- Top risk #4 (execution): uncertain exchange responses rely on reconciliation heuristics; unresolved UNKNOWN states may accumulate.
- Top risk #5-#10: endpoint schema drift, rate-limit storms, stale market data, clock skew, partial fill/accounting divergence, and missing end-to-end live simulation coverage.

## 2) Repository Overview

### 2.1 Runtime entrypoints (exact commands, main modules)
- Package entrypoint: `btcbot = "btcbot.cli:main"` (`pyproject.toml`).
- Module entrypoint: `python -m btcbot` -> `src/btcbot/__main__.py`.
- Primary commands:
  - `python -m btcbot.cli run [--dry-run|--loop|--once ...]`
  - `python -m btcbot.cli stage4-run ...`
  - `python -m btcbot.cli stage7-run ...`
  - `python -m btcbot.cli health`
  - `python -m btcbot.cli doctor`
  - replay/backtest/parity/export subcommands in `src/btcbot/cli.py`.
- Container entrypoint: Docker `ENTRYPOINT ["btcbot"]`, default `CMD ["run", "--once"]`.
- Compose runtime: `docker compose up --build` with `.env.live` and persistent `/data` volume.

### 2.2 Folder/file tree (depth 4) with 1-line purpose per file
(Condensed to operationally critical files; low-level test fixtures are grouped.)

- `README.md` — project runtime model, safety gates, and operator commands.
- `pyproject.toml` — packaging metadata, dependencies, scripts, tooling config.
- `Dockerfile` — multi-stage image build and runtime entrypoint.
- `docker-compose.yml` — local service definition with persistent state volume.
- `Makefile` — local quality gate aggregation command.
- `.github/workflows/ci.yml` — CI pipeline (lint/type/security/tests/soak/docker).
- `.env.example` / `.env.pilot.example` — environment configuration templates.
- `docs/` — architecture/runbook/threat/audit/stage design documents.
  - `docs/ARCHITECTURE.md` — replay architecture and determinism contract.
  - `docs/RUNBOOK.md` — operating procedures and incident playbooks.
  - `docs/SLO.md` — SLO and alert thresholds.
  - `docs/THREAT_MODEL.md` — threat model and risk register.
- `src/btcbot/__main__.py` — python module launcher.
- `src/btcbot/cli.py` — CLI command parser and runtime orchestration.
- `src/btcbot/config.py` — `Settings` model and env parsing/validation.
- `src/btcbot/logging_utils.py` — JSON logging setup and contextual enrichment.
- `src/btcbot/observability.py` — instrumentation setup/flush helpers.
- `src/btcbot/security/`
  - `redaction.py` — secret redaction logic for dict/text payloads.
  - `secrets.py` — secret providers, startup injection, scope/age validation.
- `src/btcbot/adapters/`
  - `exchange.py` — exchange client interface contract.
  - `btcturk_http.py` — synchronous BTCTurk REST integration + order methods.
  - `btcturk/` — async REST/WS, rate limit, retry, clock sync, reconcile helpers.
  - `replay_exchange.py` — replay dataset-backed exchange adapter.
- `src/btcbot/services/`
  - orchestration services for market data, strategy, risk, execution, allocation, universe, reconciliation, metrics, startup recovery, OMS, and state store.
- `src/btcbot/strategies/`
  - strategy interfaces and implementations (`profit_v1`, `baseline_mean_reversion`, stage5 core).
- `src/btcbot/accounting/`
  - accounting service and deterministic ledger replay models.
- `src/btcbot/domain/`
  - typed domain models for intents/orders/risk/accounting/universe/stage flows.
- `src/btcbot/replay/`
  - replay dataset tooling and schema validation.
- `tests/`
  - comprehensive unit/integration suites for adapters/services/risk/strategy/security/replay/stage flows.
  - `tests/chaos/` — resilience/chaos scenarios.
  - `tests/soak/` — long-running soak behavior checks.

### 2.3 Configuration inventory
#### ENV vars (name, required/optional, default, where used)
- Canonical source is `src/btcbot/config.py` `Settings`; examples in `.env.example`.
- Required for private BTCTurk endpoints/live: `BTCTURK_API_KEY` (optional unless private), `BTCTURK_API_SECRET` (optional unless private), `LIVE_TRADING_ACK` (required value when live arming).
- Core safety/runtime defaults:
  - `KILL_SWITCH=true`, `DRY_RUN=true`, `LIVE_TRADING=false`, `SAFE_MODE=true`.
  - `STATE_DB_PATH=btcbot_state.db`, `LOG_LEVEL=INFO`.
- Core execution knobs: `TARGET_TRY`, `OFFSET_BPS`, `TTL_SECONDS`, `MIN_ORDER_NOTIONAL_TRY`, caps and cooldown parameters.
- BTCTurk reliability/rate/WS knobs: retry delays, rps/burst, connection limits, market-data freshness thresholds.
- Stage4/Stage7 and universe/agent controls: extensive optional knobs in `Settings` (Stage7 limits, adaptation, universe selection, agent policy and LLM controls).
- UNKNOWN (complete exhaustive table): a full machine-generated env-variable usage matrix across every callsite is not present in repo; evidence missing is an authoritative generated inventory artifact.

#### Config files (YAML/JSON/TOML) and schema
- `pyproject.toml`: build metadata, deps, script entrypoint, pytest/ruff config.
- JSON inputs accepted by CLI and parsers:
  - `--pair-info-json` for backtest parity metadata.
  - env vars like `SYMBOLS`, `UNIVERSE_*` lists accept JSON list or CSV forms.
- Replay dataset CSV schemas defined in `docs/ARCHITECTURE.md` and enforced by `src/btcbot/replay/validate.py`.
- No YAML runtime config file was found.

#### Secrets handling approach
- Provider chain: environment first, then optional dotenv file (`build_default_provider`).
- Runtime injection only if variable absent in current env.
- Validation checks: scope least-privilege (`withdraw` forbidden), required scopes (`read`, `trade` for live), and rotation-age policy from timestamp.
- Redaction: JSON formatter passes payloads through redaction routines before output.

## 3) Architecture

### 3.1 High-level component diagram (text)
```
[CLI/Main]
   -> [Settings + Secret Controls + Logging + Observability]
   -> [Process Lock]
   -> [Exchange Adapter (BTCTurk or Replay)]
   -> [Services Layer]
        MarketData -> Portfolio/Accounting -> Strategy/Decision -> Risk -> Execution/OMS
   -> [StateStore SQLite + Metrics + Audit]
```

### 3.2 Control flow (startup -> run loop -> shutdown)
1. Parse CLI args and load `Settings`.
2. Inject/validate secrets and live-side-effect policy gates.
3. Initialize logging/instrumentation and state store.
4. Acquire single-instance process lock.
5. Build exchange clients/services and run startup recovery.
6. Per cycle: market/account fetch -> generate intents/actions -> risk filter -> execute/simulate -> persist metrics/state.
7. On exit: flush observability and close resources best-effort.

### 3.3 Data flow
#### Market data ingestion
- Sync flow: REST orderbook/ticker/exchangeinfo via `BtcturkHttpClient`.
- Optional async WS flow via `BtcturkWsClient` with queue, dispatch handlers, reconnect backoff.
- Replay flow: CSV-backed deterministic snapshots via `MarketDataReplay`/`replay_exchange`.

#### Signal/decision pipeline
- Stage3: `StrategyService.generate` builds `StrategyContext` and calls strategy.
- Stage5+/Stage7: `DecisionPipelineService.run_cycle` selects universe -> strategy intents -> allocation -> action-to-order mapping.
- Optional agent policy layer applies guardrails/fallback behavior.

#### Order execution pipeline
- `ExecutionService.execute_intents` refreshes lifecycle -> validates -> dedupe action record -> submit/cancel via exchange adapter.
- On uncertain errors, reconciliation path attempts order state resolution.
- Side effects blocked by dry-run/safe-mode/kill-switch/live-arm checks.

#### Position/accounting updates
- `AccountingService.refresh` ingests fills and updates positions/unrealized PnL from marks.
- Stage7 accounting can replay deterministic ledger events for full portfolio state.
- State persisted through `StateStore` tables.

### 3.4 Concurrency model
- Predominantly synchronous runtime loop for Stage3/Stage4 paths.
- Async components exist in BTCTurk adapter (`rest_client.py`, `ws_client.py`) and use `asyncio` queue/tasks.
- Shared persistent state is SQLite (WAL, busy timeout); transactional context manager provided.
- Process-level mutual exclusion uses file lock (`single_instance_lock`) keyed by DB path/account key.
- No explicit multi-process message queue framework observed.

## 4) Exchange Integration (BTC Turk)

### 4.1 API usage map (endpoints, auth method, rate limits handling)
- Public REST:
  - `/api/v2/server/exchangeinfo`
  - `/api/v2/orderbook`
  - `/api/v2/ticker`
- Private REST:
  - `/api/v1/users/balances`
  - `/api/v1/openOrders`
  - `/api/v1/allOrders`
  - `/api/v1/order` (POST submit, DELETE cancel)
  - `/api/v1/order/{order_id}`
  - `/api/v1/users/transactions/trade`
- Auth: `build_auth_headers` with API key/secret and monotonic timestamp/nonce.
- Reliability: retry with exponential backoff and `Retry-After` parsing for 429/5xx/timeout/transport classes.
- Additional async helpers exist for per-call retry/rate-limit/clock sync in `src/btcbot/adapters/btcturk/`.

### 4.2 Order lifecycle handling
- Create: submit methods in BTCTurk HTTP adapter; validation and symbol normalization precede send.
- Cancel: stale-order cancellation path with dedupe and metadata recording.
- Replace: explicit native replace endpoint not identified; behavior appears as cancel + new submit (UNKNOWN if fully implemented elsewhere).
- Partial fills/rejections/timeouts:
  - Fills polled from trade transactions endpoint and applied into accounting.
  - Rejections surfaced through HTTP error payload checks.
  - Timeouts/uncertain responses trigger reconcile flow and potential UNKNOWN order status.

### 4.3 Idempotency & reconciliation
- Dedupe via `actions` table (`payload_hash` + unique `dedupe_key`) and intent idempotency key storage.
- `client_order_id` generation/matching utilities support reconciling exchange/local records.
- Recovery/lifecycle refresh maps open/all orders and updates local status.
- Mismatch correction: unresolved uncertain outcomes can be marked `UNKNOWN`, then revisited by refresh/reconcile routines.

## 5) Strategy & Agent Logic

### 5.1 Strategy interfaces/abstractions (base classes, contracts)
- Strategy contract is protocol in `src/btcbot/strategies/base.py` (`generate_intents`).
- Context contract in `src/btcbot/strategies/context.py` and stage5 domain models.
- Decision registry/abstractions in Stage5 core (`strategies/stage5_core.py`) and pipeline service.

### 5.2 Decision-making logic
- Stage3 reference strategy (`ProfitAwareStrategyV1`):
  - Inputs: symbols, orderbook bid/ask, current positions, TRY balance, settings.
  - Rules: take-profit sell at `min_profit_bps`; otherwise conservative buy if spread <=1% and budget available.
- Stage5/7 pipeline:
  - Universe selection + baseline mean-reversion + allocation sizing + order mapping.
  - Parameters controlled via settings and bounds/adaptation services.

### 5.3 Risk management
- Position/order/cycle controls in `RiskPolicy` and higher-level services:
  - `max_orders_per_cycle`, `max_open_orders_per_symbol`, cooldown, min_notional, notional caps.
  - live-arm checks, safe-mode, kill-switch, observe-only enforcement.
- Agent guardrails enforce:
  - max exposure/notional/spread, drawdown threshold, stale-data and cooldown checks, allowlist filtering.
- Stage7 risk budgets and anomaly detection services add additional circuit/degrade signals.

### 5.4 Self-funding mechanics
- Present in Stage7 accounting + ledger services:
  - tracks trading capital, treasury, realized/unrealized pnl, fees/funding/slippage.
  - includes transfer/rebalance/withdrawal event handling.
- Compounding/capital-allocation effect appears via investable cash and allocation knobs; explicit “self-funded compounding formula” is distributed across allocation/accounting services.
- UNKNOWN: single canonical document defining exact compounding policy hierarchy for all stages is missing.

## 6) State, Persistence, and Accounting

### 6.1 What state exists (positions, orders, pnl, balances)
- Orders/actions/intents/fills/positions/meta.
- Stage4/7 extensions: ledger events, risk decisions, anomaly events, metrics, adaptation params, audit records.
- Latest balances and snapshots persisted for decision/risk/accounting usage.

### 6.2 Where state is stored (memory/db/files)
- Durable state: SQLite file path `STATE_DB_PATH` (default `btcbot_state.db`).
- Ephemeral state: in-memory service context structures per cycle.
- Replay/backtest datasets: filesystem CSV/JSON artifacts under `data/replay` or operator-specified paths.

### 6.3 Recovery after crash/restart (exact steps + what can go wrong)
- Startup recovery service performs:
  1) order lifecycle refresh,
  2) fill refresh/accounting update (if marks available),
  3) invariants check (negative balances/positions),
  4) returns observe-only requirement if anomalies/incomplete data detected.
- Failure modes:
  - missing marks -> observe-only reason set,
  - private endpoint failure -> stale local state,
  - unresolved unknown orders -> delayed reconciliation,
  - SQLite contention/corruption risk (mitigated by WAL + busy timeout, but not eliminated).

### 6.4 PnL calculation method and edge cases (fees, partial fills)
- Stage3 accounting:
  - BUY updates weighted avg cost + fees (quote-currency fees only).
  - SELL realizes pnl on matched quantity, prorates fee usage for partial sells, resets avg cost when flat.
  - Unrealized pnl uses mark minus avg cost times qty.
- Edge cases:
  - non-quote fee currency is ignored with warning (risk of pnl distortion).
  - oversell prevented in deterministic ledger path via exception.
  - partial fills depend on fill ingestion quality and idempotent fill IDs.

## 7) Observability & Operations

### 7.1 Logging (format, levels, rotation, sensitive data redaction)
- Structured JSON logs via `JsonFormatter`.
- Level from `LOG_LEVEL`, with environment overrides for httpx/httpcore sub-loggers.
- Redaction applied to payload keys and plain-text token patterns.
- Rotation policy: UNKNOWN (no explicit file-rotation component; stdout/stderr likely delegated to runtime platform).

### 7.2 Metrics & health checks (latency, order success, pnl, errors)
- Instrumentation wrappers expose traces/histograms/counters (REST calls, cancel latency, ws reconnect/backpressure/errors).
- Health/doctor commands provide environment/connectivity and dataset checks.
- Stage7 metrics collectors/services persist cycle stats and anomaly indicators.

### 7.3 Alerts (what triggers, where sent)
- Trigger thresholds documented in `docs/SLO.md` for reconnect/stale/reconcile/order latency.
- Destination/notification transport is UNKNOWN (no built-in pager integration found in repository).

### 7.4 Deployment model (local/vps/docker/k8s)
- Confirmed: local venv and Docker/Compose flows.
- VPS/K8s manifests: UNKNOWN (not found in repository).

### 7.5 Runbook (how to start/stop, upgrade, rollback)
- Start: `btcbot run`/`stage4-run` with safe defaults.
- Stop: process termination with flush/close best-effort.
- Upgrade/rollback and emergency disable procedures are documented in `docs/RUNBOOK.md`.

## 8) Testing & Quality

### 8.1 Test suite inventory (unit/integration/e2e)
- Extensive unit tests across adapters/domain/services/risk/strategy/security/state.
- Integration-style tests include btcturk ws/retry and stage runners.
- Chaos tests in `tests/chaos/`; soak tests in `tests/soak/`.
- True live-exchange E2E tests: UNKNOWN/limited (most tests appear mocked/fixture-based).

### 8.2 How exchange calls are mocked
- Test fixtures and mock transports/fake clients are used (e.g., btcturk HTTP/WS tests, replay exchange tests).
- Replay dataset tooling supports deterministic backtest scenarios.

### 8.3 Determinism and reproducibility
- Replay contract and parity tooling designed for deterministic backtest fingerprints.
- Seeded replay generation and fixed dataset windows support reproducibility.
- CI executes deterministic quality gates and segmented test suites.

### 8.4 Missing tests (prioritized list)
1. P0: End-to-end crash-recovery with unresolved UNKNOWN orders + subsequent reconciliation closure.
2. P0: Multi-cycle financial correctness under mixed fee currencies (quote vs non-quote).
3. P0: Live-arm policy regression matrix for all safety flag combinations.
4. P1: Extended rate-limit storm with partial exchange schema drift.
5. P1: SQLite corruption/recovery and backup restore drills.
6. P1: Full Stage3->Stage4->Stage7 compatibility/invariant migration tests.
7. P2: Long-horizon performance profiling assertions (latency budget under load).

## 9) Security Review

### 9.1 Secret management and leakage risks
- Strengths: centralized providers, scope/rotation checks, redaction utilities, CI bandit scan scope.
- Risks: dotenv plaintext storage, environment leakage at host level, possible accidental committed DB/artifacts containing sensitive operational metadata.

### 9.2 Input validation, injection, unsafe deserialization
- Uses pydantic settings and model validators for many runtime inputs.
- JSON parsing exists for env/CLI/WS payloads with schema checks in key paths.
- No obvious unsafe YAML/pickle deserialization found.
- SQL uses parameterized queries via sqlite3 API patterns; raw SQL schema DDL is static.

### 9.3 Dependency risks (requirements, pinned versions)
- Dependencies are pinned in `pyproject.toml`.
- Constraints file exists; CI installs pinned deps.
- UNKNOWN: SBOM generation and automated CVE monitoring status are not explicitly configured.

### 9.4 Permissions, key scope, least privilege
- API scopes include read/trade defaults and explicit rejection of withdraw scope.
- Live side effects require explicit multi-flag arming and acknowledgment.
- Process/user hardening in Docker uses non-root runtime user.

### 9.5 Supply chain concerns and CI/CD security
- CI runs lint/type/tests/bandit and scheduled soak.
- Missing hardening items: dependency provenance attestation, signed releases, secret scanning gates, branch protection policy evidence (UNKNOWN in-repo).

## 10) Performance & Reliability

### 10.1 Bottlenecks (I/O, CPU, network)
- Dominant bottlenecks are network I/O (BTCTurk REST/WS latency and throttling) and SQLite write contention under high event volume.
- CPU-bound logic is comparatively light; strategy/risk loops are simple relative to I/O.

### 10.2 Rate limiting/backoff strategy
- HTTP retries with capped exponential backoff and `Retry-After` handling for 429.
- Additional rate-limit module and retry utilities in btcturk adapter package.
- WS reconnect backoff with jitter and idle-timeout reconnect.

### 10.3 Failure modes & mitigation
- Network flaps / exchange downtime:
  - retry/backoff, observe-only/safe-mode controls, runbook incident flow.
- Stale data / clock skew:
  - stale thresholds, anomaly/risk controls, clock sync helper modules.
- Duplicated orders / inconsistent state:
  - idempotency keys + dedupe tables + reconciliation and startup recovery.
- Residual risk:
  - prolonged UNKNOWN states, exchange-side eventual consistency, operator mis-arming.

## 11) Concrete Action Plan
- [Priority P0] [Effort M] [src/btcbot/services/state_store.py + ops docs] Add encrypted-at-rest or host-level mandatory encryption guidance for `STATE_DB_PATH` and prohibit committing runtime DB artifacts.
  - Acceptance Criteria: CI fails if `*.db` tracked; runbook includes encryption/backup requirements; pre-commit guard added.
- [Priority P0] [Effort M] [src/btcbot/accounting/accounting_service.py] Implement deterministic handling/conversion policy for non-quote fee currencies instead of ignore-with-warning.
  - Acceptance Criteria: tests cover fee currency variants; pnl deltas match documented formula.
- [Priority P0] [Effort S] [src/btcbot/cli.py + tests] Add exhaustive safety-flag truth-table test and startup printout for effective side-effect state.
  - Acceptance Criteria: each flag combination has deterministic allow/deny result in tests.
- [Priority P0] [Effort M] [src/btcbot/services/execution_service.py] Add bounded UNKNOWN-order retry policy with escalation to alert state.
  - Acceptance Criteria: UNKNOWN aging thresholds tested; alertable metric emitted.
- [Priority P0] [Effort M] [docs + scripts] Add automated DB backup/restore drill command and integrity check in runbook.
  - Acceptance Criteria: scripted restore verified in CI/nightly smoke.
- [Priority P1] [Effort M] [src/btcbot/config.py] Generate and ship machine-readable env schema inventory from `Settings` (name/default/type/description).
- [Priority P1] [Effort M] [src/btcbot/adapters/btcturk_http.py] Centralize endpoint contract validation with explicit typed response schemas.
- [Priority P1] [Effort M] [src/btcbot/services/startup_recovery.py] Enrich startup recovery with partial-service degradation modes and richer invariant taxonomy.
- [Priority P1] [Effort S] [docs/RUNBOOK.md] Add explicit SRE on-call escalation matrix and channel routing.
- [Priority P1] [Effort M] [CI workflow] Add dependency vulnerability scanning (e.g., `pip-audit`) and license checks.
- [Priority P1] [Effort M] [security] Add secret scanning in CI and denylist checks for committed `.env`/credentials.
- [Priority P1] [Effort L] [stage integration] Add cross-stage contract/invariant integration suite for Stage3/4/7.
- [Priority P1] [Effort M] [observability] Persist key SLO metrics to time-series backend adapter with retention policy.
- [Priority P1] [Effort S] [Docker/K8s] Provide production deployment manifests and hardening baseline (seccomp/read-only FS).
- [Priority P1] [Effort S] [src/btcbot/security/secrets.py] Add optional external secret manager provider (Vault/KMS).
- [Priority P2] [Effort S] [tests/perf] Add repeatable latency benchmark harness.
- [Priority P2] [Effort M] [decision pipeline] Add explainability artifacts for intent-to-order transformations.
- [Priority P2] [Effort M] [replay] Add dataset drift detector and schema migration assistant.
- [Priority P2] [Effort S] [docs] Add module ownership map and CODEOWNERS.
- [Priority P2] [Effort M] [accounting/ledger] Add stress tests for extreme precision/rounding cases.
