# Architecture Map (Repository-wide)

## Tree

```text
.
├── .github/
│   └── workflows/ci.yml
├── data/
│   ├── README.md
│   └── replay/README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── pilot_live.md
│   ├── planning_kernel_refactor.md
│   ├── RUNBOOK.md
│   ├── stage4.md
│   ├── stage6_2_metrics_and_atomicity.md
│   ├── stage6_3_risk_budget.md
│   ├── stage6_4_degrade_and_anomalies.md
│   ├── stage6_ledger.md
│   ├── stage7.md
│   └── STAGES.md
├── scripts/
│   ├── capture_exchangeinfo_fixture.py
│   ├── debug_stage7_metrics.py
│   ├── debug_stage7_schema.py
│   ├── dev.ps1
│   └── guard_multiline.py
├── src/
│   ├── btcbot/
│   │   ├── accounting/
│   │   ├── adapters/
│   │   │   ├── btcturk/
│   │   │   ├── action_to_order.py
│   │   │   ├── btcturk_auth.py
│   │   │   ├── btcturk_http.py
│   │   │   ├── exchange.py
│   │   │   ├── exchange_stage4.py
│   │   │   └── replay_exchange.py
│   │   ├── domain/
│   │   ├── replay/
│   │   ├── risk/
│   │   ├── services/
│   │   ├── strategies/
│   │   ├── __main__.py
│   │   ├── cli.py
│   │   ├── config.py
│   │   ├── logging_context.py
│   │   ├── logging_utils.py
│   │   ├── observability.py
│   │   └── planning_kernel.py
│   └── btcbot.egg-info/
├── tests/
│   ├── fixtures/
│   ├── conftest.py
│   └── test_*.py (stage3/stage4/stage7/adapters/services coverage)
├── .env.example
├── .env.pilot.example
├── .gitignore
├── AUDIT_REPORT.md
├── btcbot_state.db
├── check_exchangeinfo.py
├── constraints.txt
├── docker-compose.yml
├── Dockerfile
├── INTRO_MAP.md
├── Makefile
├── PROJECT_REPORT.md
├── pyproject.toml
└── README.md
```

> Excluded from tree by request: `venv`, `.git`, `node_modules`, `.pytest_cache`, `dist`, `build`.

## Entry points

- Packaging/CLI entrypoint: `btcbot = btcbot.cli:main` in `pyproject.toml`.
- Python module entrypoint: `python -m btcbot` delegates to `btcbot.cli:main` via `src/btcbot/__main__.py`.
- Primary commands in `src/btcbot/cli.py`:
  - `run` (Stage 3 cycle / optional loop)
  - `stage4-run`
  - `stage7-run` (dry-run only; requires `STAGE7_ENABLED=true`)
  - `health`, `doctor`
  - `stage7-report`, `stage7-export`, `stage7-alerts`
  - `stage7-backtest`, `stage7-parity`, `stage7-backtest-export`, `stage7-db-count`
  - `replay-init`, `replay-capture`
- Operational scripts:
  - `scripts/guard_multiline.py` quality guard
  - `scripts/capture_exchangeinfo_fixture.py` fixture capture
  - `scripts/debug_stage7_metrics.py` / `scripts/debug_stage7_schema.py` DB diagnostics.

## Runtime flow

### Stage 3 (`btcbot run`)

1. Bootstraps settings, logging, instrumentation, and effective universe.
2. Applies safety gating (`DRY_RUN`, `KILL_SWITCH`, safe mode, live-arming checks).
3. Builds exchange adapter + `StateStore`.
4. Constructs core services:
   - `PortfolioService`, `MarketDataService`, `AccountingService`, `StrategyService`, `RiskService`, `ExecutionService`, `SweepService`.
5. Reads balances + best bids; runs startup recovery.
6. Reconciles/cancels stale orders.
7. Refreshes accounting/fills.
8. Strategy generates intents.
9. Risk filters intents against limits and state.
10. Execution submits (or simulates/blocks based on gates), persists audit/actions/order status.
11. Emits metrics/logs and flushes instrumentation.

### Scheduling / market data receipt

- Main scheduler is CLI loop mode (`--loop`) with cycle sleep + optional jitter.
- Stage 3/4 data reads are pull-based via REST (`orderbook`, `ticker`, exchange info, balances, orders, fills).
- A dedicated websocket client exists under `src/btcbot/adapters/btcturk/ws_client.py`; config has `BTCTURK_WS_ENABLED`, but primary orchestration path is REST polling in cycle runners.

### Stage 4 (`stage4-run`)

1. Single-instance lock (`process_lock`) and live-write policy check.
2. `Stage4CycleRunner` orchestrates:
   - dynamic universe selection (optional)
   - account snapshot + mark prices
   - open-order reconcile and fill ingestion
   - ledger/accounting update transaction
   - decision pipeline / planning-kernel integration
   - risk budgeting + anomaly/degrade mode decision
   - lifecycle action execution via Stage 4 execution service
   - persistence of cycle artifacts and metrics.

### Stage 7 (`stage7-run`, backtest/parity)

1. Enforces dry-run and feature flag.
2. `Stage7CycleRunner` optionally executes a Stage 4 cycle first, then computes Stage 7 analytics/simulation overlays.
3. Uses universe selection, portfolio policy, order builder, risk budget, OMS market simulator, metrics collector, and adaptation service.
4. Backtest mode (`Stage7BacktestRunner`) runs deterministic replay over time windows into SQLite; parity tool compares run fingerprints between DBs.

## Dependencies

### Python/package dependencies

- Runtime: `httpx`, `pydantic`, `pydantic-settings`, `tenacity`, `python-dotenv`, `rich`, OpenTelemetry exporters, `prometheus-client`.
- Dev: `pytest`, `ruff`, `mypy`.

### External services / integrations

- **Exchange**: BTCTurk REST (`https://api.btcturk.com`) and optional websocket URL (`wss://ws-feed-pro.btcturk.com`).
- **Database**: SQLite (`StateStore`, backtest/parity/report tooling).
- **Telemetry**: OpenTelemetry OTLP endpoint (optional), Prometheus exporter HTTP endpoint (optional).
- **Replay dataset storage**: local filesystem datasets (`data/replay`); used by replay/backtest services.

### Not present in runtime wiring

- No Redis, Kafka, RabbitMQ, Postgres, MongoDB, or external message bus clients found in runtime architecture.
- No LLM API or chat/messaging integrations (OpenAI/Anthropic/Telegram/Slack) found.

## Component map

- `btcbot.cli`
  - **Responsibility**: command routing, loop scheduling, lifecycle bootstrapping.
  - **Key functions**: `main`, `run_cycle`, `run_cycle_stage4`, `run_cycle_stage7`, `run_with_optional_loop`.

- `btcbot.config.Settings`
  - **Responsibility**: env/settings schema and validation for all stages.
  - **Key methods**: symbol parsing/normalization, live-arm checks.

- `btcbot.adapters.*`
  - **Responsibility**: exchange IO adapters (REST auth, order/market endpoints, stage interfaces, replay adapters).
  - **Key classes**: `BtcturkHttpClient`, `ReplayExchangeClient`, Stage4 exchange adapters.

- `btcbot.services.state_store`
  - **Responsibility**: SQLite schema management + persistence for actions/orders/ledger/stage4/stage7 metrics.
  - **Key class**: `StateStore` (transaction boundaries + most persistence APIs).

- Stage 3 pipeline services
  - `MarketDataService` -> market reads + rules cache.
  - `AccountingService` -> fill ingestion/positions.
  - `StrategyService` -> strategy context + intent generation.
  - `RiskService`/`risk.policy` -> intent filtering and risk enforcement.
  - `ExecutionService` -> order lifecycle, submit/cancel, idempotency and reconciliation.
  - `StartupRecoveryService`/`SweepService` -> startup healing + sweep intents.

- Stage 4 orchestration
  - `Stage4CycleRunner` (top-level cycle orchestrator).
  - `DecisionPipelineService`, `RiskBudgetService`, `AnomalyDetectorService`, `OrderLifecycleService`, `ReconcileService`, `ExecutionService` (stage4), `LedgerService`, planning-kernel adapters.

- Stage 7 analytics/backtest
  - `Stage7CycleRunner`, `Stage7BacktestRunner`.
  - `OMSService`/`Stage7MarketSimulator`, `MetricsCollector`, `AdaptationService`, `UniverseSelectionService`, `PortfolioPolicyService`.
  - `parity.py` + CLI parity/export/report commands.

## Risk / tech-debt flags

- **God modules by size/concern density**
  - `src/btcbot/services/state_store.py` (~3452 LOC): schema creation, migrations, and broad persistence surface in one class.
  - `src/btcbot/cli.py` (~1537 LOC): command parsing + orchestration + reporting + DB helpers.
  - `src/btcbot/adapters/btcturk_http.py` (~1406 LOC): large multi-concern adapter (transport, retries, parsing, domain mapping).
  - `src/btcbot/services/stage4_cycle_runner.py` and `stage7_cycle_runner.py` (~1100 LOC each): heavy orchestration with many responsibilities.

- **Potential duplication / overlap**
  - Multiple risk-policy layers: `btcbot/risk/policy.py` (Stage 3) and `services/risk_policy.py` (Stage 4), plus stage7 risk-budget service; expected by stage evolution but increases cognitive load.
  - Execution split across `execution_service.py` (Stage 3) and `execution_service_stage4.py` (Stage 4), plus `oms_service.py` (Stage 7 simulation).
  - Exchange integration split between legacy monolith adapter (`btcturk_http.py`) and newer subpackage modules under `adapters/btcturk/*`.

- **Dead-code candidates (needs confirmation via coverage/runtime traces)**
  - Config exposes websocket toggles, while primary cycle runners are REST-poll centric.
  - Presence of both old/new adapter layers suggests partial migration; confirm active call graph before deleting.
  - Several diagnostic scripts appear one-off/operator-only and may not be integrated into CI workflows.
