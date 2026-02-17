# Repository Inventory Report

## A) Repo tree map (top-level + important subfolders)

Top-level directories and files (selected):
- `.github/workflows/ci.yml` (CI pipeline)
- `src/btcbot/` (application package)
- `tests/` (unit/integration/chaos/soak tests)
- `docs/` (architecture/runbook/stage docs)
- `data/replay/` (replay dataset area)
- `scripts/` (guard and debugging utilities)
- `Dockerfile`, `docker-compose.yml`, `Makefile`, `pyproject.toml`, `constraints.txt`, `.env.example`, `.env.pilot.example`.

Important `src/btcbot` package boundaries:
- `adapters/` and `adapters/btcturk/`: exchange HTTP/WS and integration utilities
- `services/`: orchestration and lifecycle services (market/accounting/strategy/risk/execution/state)
- `domain/`: typed models/contracts for orders, risk, universe, accounting, etc.
- `strategies/`: strategy implementations
- `accounting/`: accounting service and ledger models
- `risk/`: risk policy and exchange rules provider
- `replay/`: replay dataset tooling and validation
- `security/`: secrets and redaction utilities
- top-level `cli.py`, `config.py`, `__main__.py`.

## B) Runtime components (processes/services) and how they start

Primary process model:
- Single Python CLI process (`btcbot`) exposed via `project.scripts` and `python -m btcbot.cli` / `python -m btcbot`.
- No web server entrypoint is defined; runtime is command/subcommand-driven.

Entrypoints and startup:
- Console script: `btcbot = btcbot.cli:main`.
- Module entrypoint: `src/btcbot/__main__.py` calls `btcbot.cli.main()`.
- Docker runtime entrypoint: `ENTRYPOINT ["btcbot"]`, default `CMD ["run", "--once"]`.
- Compose service starts loop mode: `command: ["run", "--loop", "--cycle-seconds", "10"]`.

CLI runtime commands (selected, from parser):
- Stage3-like: `run`, `health`, `doctor`
- Stage4: `stage4-run`
- Stage7: `stage7-run`, `stage7-backtest`, `stage7-parity`, `stage7-report`, `stage7-export`, `stage7-alerts`

Scheduling model:
- Internal loop scheduling is in CLI command options (`--loop`, `--cycle-seconds`, `--max-cycles`, optional jitter).
- No external scheduler/systemd unit file is present in the repository.

## C) Key modules and responsibilities

Core runtime wiring:
- `btcbot.cli`: argparse command surface and orchestration for stage runs, health/doctor, replay/backtest/parity commands.
- `btcbot.config.Settings`: centralized environment-backed config schema (`BaseSettings`) with validation/parsing.

Exchange connectors:
- `btcbot.adapters.btcturk_http.BtcturkHttpClient`: synchronous BTCTurk REST client (public/private endpoints, retries, auth integration).
- `btcbot.adapters.btcturk.ws_client.BtcturkWsClient`: async websocket client abstraction with reconnect/backoff, queueing, handler dispatch.
- `btcbot.services.exchange_factory`: builds stage3/stage4 exchange clients and dry-run/live variants.

Data/storage and replay:
- `btcbot.services.state_store.StateStore`: SQLite persistence layer; initializes schema and provides transactional operations.
- `btcbot.replay.validate`: replay dataset contract checks.
- `btcbot.services.market_data_replay` + `btcbot.adapters.replay_exchange`: replay-mode market/exchange plumbing.

Strategy/risk/execution layering:
- `btcbot.services.strategy_service` and `btcbot.strategies.*`: strategy signal/intent production.
- `btcbot.services.risk_service` + `btcbot.risk.policy`: risk filtering and constraints.
- `btcbot.services.execution_service` (+ `execution_service_stage4`): order lifecycle handling and execution path.
- `btcbot.services.decision_pipeline_service`: orchestration of universe -> strategy -> allocation -> order mapping.
- `btcbot.services.stage4_cycle_runner` and `stage7_cycle_runner`: high-level per-cycle orchestrators for stage modes.

Accounting/ledger:
- `btcbot.accounting.accounting_service`: applies fills and computes accounting state.
- `btcbot.accounting.ledger` and `btcbot.services.ledger_service`: ledger-domain processing and integration.

## D) External dependencies list with purpose (present in repo)

From `pyproject.toml` and pinned in `constraints.txt`:
- `httpx`: HTTP client for exchange/API communication.
- `pydantic`, `pydantic-settings`: settings and typed model validation.
- `tenacity`: retry utilities.
- `python-dotenv`: dotenv loading support.
- `rich`: CLI output formatting.
- `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-exporter-prometheus`, `prometheus-client`: observability/metrics instrumentation.
- Dev/test: `pytest`, `ruff`, `mypy`.

Notes:
- `requirements*.txt` files are not present; dependency declarations are in `pyproject.toml` + `constraints.txt`.
- `src/btcbot.egg-info/requires.txt` exists but contains metadata ranges that differ from pinned constraints.

## E) Configuration sources, infrastructure files, and explicit unknowns

Configuration sources (evidence-based):
- Environment and dotenv through `Settings` (`env_file=".env.live"`, many `alias=ENV_VAR`).
- Example env profiles: `.env.example`, `.env.pilot.example`.
- CLI accepts `--env-file` for dotenv bootstrap.
- `pyproject.toml` and `constraints.txt` for packaging/dependency configuration.
- JSON is accepted by specific CLI/config fields (e.g., `--pair-info-json`, symbol list parsing); no runtime YAML config file discovered.

Infra/runtime files:
- Dockerfile (multi-stage build, non-root runtime user, default CLI command).
- docker-compose service using `.env.live` and persistent `/data` volume.
- GitHub Actions CI (`.github/workflows/ci.yml`) with static analysis, tests, integration fixtures, soak schedule, and docker build.
- No systemd unit files found.

Explicit unknowns / missing info:
- No documented production process manager beyond Docker/Compose; systemd/K8s manifests are not present.
- No dedicated secrets manager integration config is shown (only env/dotenv + runtime secret validation helpers).
- WebSocket transport implementation dependency is abstracted in code via injected `connect_fn`; concrete library choice is not declared in dependency pins shown.
