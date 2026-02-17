# Repository Inventory Audit (evidence-only)

## A) Repo tree map

### Top-level
- `.github/workflows/` (CI pipeline)
- `data/` (data + replay dataset docs)
- `docs/` (architecture/runbook/stage docs)
- `scripts/` (utility scripts)
- `src/btcbot/` (main Python package)
- `tests/` (unit/integration/chaos/soak tests)
- Root artifacts and config: `pyproject.toml`, `constraints.txt`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.env.pilot.example`, `Makefile`.

### Important subfolders in `src/btcbot`
- `accounting/`: accounting models/services/ledger implementation.
- `adapters/`: exchange integrations (BTCTurk HTTP/WS/replay), adapter interfaces.
- `agent/`: policy/guardrails/audit/contracts.
- `domain/`: typed domain models.
- `replay/`: replay dataset tooling/validation.
- `risk/`: risk policy and exchange-rule logic.
- `security/`: secrets and redaction utilities.
- `services/`: orchestration layer (market data, strategy, risk, execution, stage4/stage7 runners, state store, etc.).
- `strategies/`: strategy implementations.

## B) Runtime components (processes/services) and how they start

### Entrypoints
- Installed CLI entrypoint is `btcbot = btcbot.cli:main`.
- Module entrypoint `python -m btcbot` delegates to `btcbot.cli:main`.

### CLI processes
- Primary CLI commands include: `run`, `stage4-run`, `stage7-run`, `health`, `doctor`, `stage7-report`, `stage7-export`, `stage7-alerts`, `stage7-backtest`, `stage7-parity`, `replay-init`, `replay-capture`, plus Stage7 export/count helpers.
- Loop scheduling is via CLI flags (`--loop`, `--cycle-seconds/--sleep-seconds`, `--max-cycles`, `--jitter-seconds`).

### Stage pipelines and safety gating
- README states default pipeline: `MarketData + Portfolio + Reconcile/Fills -> Accounting -> Strategy -> Risk -> Execution`.
- Triple-gate live-safety controls are documented: `DRY_RUN`, `KILL_SWITCH`, and live arming requirements (`LIVE_TRADING=true` + `LIVE_TRADING_ACK=I_UNDERSTAND` with `DRY_RUN=false`, `KILL_SWITCH=false`).

### Runtime services/components visible in code
- Stage 3 exchange bootstrap (`build_exchange_stage3`) returns either dry-run exchange wrapper or BTCTurk HTTP client.
- Stage 4 exchange bootstrap (`build_exchange_stage4`) wraps Stage 3 dry-run for simulation or uses live BTCTurk HTTP client.
- Stage 7 runner enforces dry-run only and composes Stage4 runner, state store, metrics collector, adaptation service, universe/policy/order/risk budget services.

### Containerized startup
- Docker image entrypoint is `btcbot` with default command `run --once`.
- Docker Compose service `btcbot` runs command `run --loop --cycle-seconds 10`, loads `.env.live`, and mounts `/data` volume.

## C) Key modules and responsibilities

- `src/btcbot/cli.py`: command parsing + orchestration entrypoint for run/stage4/stage7/doctor/replay commands.
- `src/btcbot/config.py`: centralized settings model (env + dotenv) with aliases for runtime knobs.
- `src/btcbot/services/exchange_factory.py`: builds stage-specific exchange clients (dry-run vs live BTCTurk HTTP).
- `src/btcbot/services/state_store.py`: SQLite persistence layer (schema bootstrap, WAL setup, transactions, lifecycle/risk/stage7 tables and state).
- `src/btcbot/services/market_data_service.py`: orderbook reads and symbol-rules caching/lookup.
- `src/btcbot/services/strategy_service.py`: builds strategy context and invokes strategy to generate intents.
- `src/btcbot/services/risk_service.py`: runs risk policy filtering with store-derived context and intent recording.
- `src/btcbot/services/execution_service.py`: order lifecycle refresh/cancel/submit execution path with live-arming/kill-switch/dry-run controls.
- `src/btcbot/services/stage4_cycle_runner.py`: Stage 4 orchestrator composing decision/risk/execution/reconcile/accounting/ledger/universe services.
- `src/btcbot/services/stage7_cycle_runner.py`: Stage 7 dry-run orchestration with adaptation + risk budget + stage4 integration.
- `src/btcbot/services/stage7_backtest_runner.py`: replay-driven Stage7 backtest runner, summary/fingerprint output.
- `src/btcbot/adapters/exchange.py`: exchange interface contract.
- `src/btcbot/adapters/btcturk_http.py`: sync BTCTurk HTTP adapter with retry, auth, and endpoint calls.
- `src/btcbot/adapters/btcturk/rest_client.py`: async REST client with reliability policy, retries, idempotent-safe submit/cancel semantics.
- `src/btcbot/adapters/btcturk/ws_client.py`: websocket client with reconnect/backoff/dispatch/heartbeat queue.
- `src/btcbot/adapters/replay_exchange.py` + `src/btcbot/services/market_data_replay.py`: replay dataset-backed exchange simulation.

## D) External dependencies present and purpose (from repository metadata/docs)

- `httpx`: HTTP transport client for exchange connectivity.
- `pydantic`, `pydantic-settings`: typed settings/config parsing and validation.
- `tenacity`: retry utilities.
- `python-dotenv`: dotenv support.
- `rich`: richer CLI output.
- `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-exporter-prometheus`, `prometheus-client`: observability/metrics instrumentation.
- Dev/test/tooling deps: `pytest`, `ruff`, `mypy`.
- CI additionally installs `bandit` for security linting.

## E) Configuration sources / secrets / infrastructure / unknowns

### Configuration sources and secrets (evidence)
- Settings load from env with default dotenv file `.env.live`.
- Example env profiles exist in `.env.example` and `.env.pilot.example`.
- Secrets and credentials are expected via env vars (`BTCTURK_API_KEY`, `BTCTURK_API_SECRET` etc.).
- Stage4/Stage7 commands can accept `--db`, otherwise use `STATE_DB_PATH`.

### Exchange connectors, storage, scheduling, strategy/risk/execution layers (evidence)
- Connectors: BTCTurk HTTP (sync + async) and websocket client modules are present.
- Storage: SQLite state store (`StateStore`) with WAL and transaction context managers.
- Scheduling: loop-based scheduler from CLI options (`--loop`, cycle/jitter/max-cycles).
- Strategy: strategy module/service (`strategies/`, `StrategyService`).
- Risk: risk policy/service modules (`risk/`, `RiskService`, risk-budget services in stage4/stage7 files).
- Execution: execution service modules (`ExecutionService`, `execution_service_stage4`, OMS-related services).

### CI / deployment / ops artifacts
- GitHub Actions CI exists with static analysis, unit tests, integration fixtures, scheduled soak tests, and docker build.
- Dockerfile and docker-compose are present.
- No systemd unit files were found in repository file listing.

### Explicit unknowns / missing info
- No production deployment manifests beyond Docker/Compose were found (e.g., Kubernetes, systemd) in tracked files.
- Runtime topology for multi-process execution is not declared as separate worker/web daemons; evidence shows a CLI-driven single process loop, but docs do not define separate long-lived worker binaries.
- Exact live exchange API permissions/scopes enforcement behavior at runtime is referenced in runbook/config but full operational policy depends on environment values not present in repository secrets.
