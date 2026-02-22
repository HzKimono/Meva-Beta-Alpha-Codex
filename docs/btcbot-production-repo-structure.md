# BTCBot Production Repository Structure Proposal (Python 3.12)

```text
btcbot/
├── pyproject.toml
├── README.md
├── .gitignore
├── .env.example                    # documentation only; runtime uses environment variables
├── scripts/
│   ├── run_live.ps1                # starts LIVE TRADE process in its own venv/session
│   ├── run_monitor.ps1             # starts MONITOR process in its own venv/session
│   ├── bootstrap_windows.ps1       # creates venvs and installs dependencies
│   └── db/
│       ├── init_state_db.py        # creates schema for given STATE_DB_PATH
│       └── check_state_db.py       # validates db path, schema version, and locks
├── src/
│   └── btcbot/
│       ├── __init__.py
│       ├── py.typed
│       ├── cli.py                  # entrypoint: run / health / arm / disarm / inspect
│       ├── settings.py             # strict env parsing, mode validation, typed settings model
│       ├── wiring.py               # dependency injection container/factories per mode
│       ├── lifecycle.py            # startup/shutdown hooks and process role guards
│       │
│       ├── domain/                 # pure business logic (no I/O)
│       │   ├── models.py           # Orders, Positions, Signals, Balances, etc.
│       │   ├── enums.py
│       │   ├── value_objects.py
│       │   └── errors.py
│       │
│       ├── strategy/
│       │   ├── engine.py           # deterministic signal generation
│       │   ├── indicators.py
│       │   ├── feature_flags.py
│       │   └── protocols.py        # strategy-facing interfaces
│       │
│       ├── exchange/
│       │   ├── client.py           # BTC Turk HTTP/WebSocket adapter
│       │   ├── auth.py
│       │   ├── dto.py
│       │   ├── mappers.py
│       │   └── rate_limit.py
│       │
│       ├── risk/
│       │   ├── engine.py           # pre-trade checks
│       │   ├── limits.py           # max exposure, notional caps, cooldowns
│       │   ├── kill_switch.py      # MONITOR-safe hard block + emergency disarm
│       │   └── policies.py
│       │
│       ├── execution/
│       │   ├── service.py          # only place allowed to submit/cancel exchange orders
│       │   ├── order_builder.py
│       │   ├── slippage.py
│       │   └── protocols.py        # execution interfaces for tests
│       │
│       ├── state/
│       │   ├── db/
│       │   │   ├── schema.sql
│       │   │   ├── migrate.py
│       │   │   └── sqlite.py       # sqlite connection factory, pragma, busy timeout
│       │   ├── repositories/
│       │   │   ├── readonly.py     # read models used by monitor + strategy context
│       │   │   ├── orders_repo.py
│       │   │   ├── positions_repo.py
│       │   │   ├── risk_repo.py
│       │   │   └── events_repo.py
│       │   ├── uow.py              # unit-of-work transaction boundary
│       │   └── guards.py           # startup checks: role/db-path compatibility
│       │
│       ├── services/
│       │   ├── run_loop.py         # loop orchestration for run --loop
│       │   ├── health_service.py   # health command and checks
│       │   └── reconciliation.py
│       │
│       ├── observability/
│       │   ├── logging.py          # structlog/logging config
│       │   ├── metrics.py          # prometheus counters/histograms
│       │   ├── tracing.py
│       │   └── audit.py            # immutable audit events
│       │
│       └── adapters/
│           ├── clock.py
│           ├── uuid_gen.py
│           └── filesystem.py
│
├── tests/
│   ├── unit/
│   │   ├── test_strategy_engine.py
│   │   ├── test_risk_engine.py
│   │   ├── test_execution_service.py
│   │   └── test_settings.py
│   ├── integration/
│   │   ├── test_run_loop_live_mode.py
│   │   ├── test_health_monitor_mode.py
│   │   ├── test_sqlite_repositories.py
│   │   └── test_db_role_guards.py
│   ├── e2e/
│   │   ├── test_live_process_smoke.py
│   │   └── test_monitor_process_smoke.py
│   ├── fixtures/
│   │   ├── fake_exchange.py
│   │   ├── state_db_factory.py
│   │   └── sample_market_data.json
│   └── conftest.py
│
└── tools/
    ├── mypy.ini
    ├── ruff.toml
    ├── pytest.ini
    └── pre-commit-config.yaml
```

- Split runtime into explicit process roles (`APP_ROLE=live|monitor`) validated in `settings.py` and enforced by `state/guards.py`, so each PowerShell session must supply a distinct `STATE_DB_PATH` and cannot start with an incompatible role/database pairing.
- Restrict DB writes to `state/repositories/*` behind `state/uow.py`; all other modules consume protocols or read models. `strategy/` and `risk/` remain pure and deterministic for mypy-friendly unit tests.
- Keep exchange integration isolated under `exchange/` and map external DTOs into internal domain models immediately, minimizing coupling to BTC Turk API schema changes.
- Place all order placement/cancellation logic in `execution/service.py` so the “can trade” boundary is one module, with risk and kill-switch checks required before any side effect.
- Use `risk/kill_switch.py` and role-aware wiring to force MONITOR mode into non-trading behavior even if upstream code accidentally calls execution paths.
- Centralize dependency wiring in `wiring.py` to instantiate different implementations per mode (e.g., `NoopExecutionService` for monitor), improving testability and reducing conditional logic spread.
- Add startup lifecycle checks (`lifecycle.py`) that fail fast on missing env vars, wrong DB file, schema mismatch, or stale lock files; this prevents silent DB mixing across sessions.
- Keep logging/metrics/tracing in `observability/` with structured event IDs and audit trails so live and monitor processes can be correlated without sharing mutable state.
- Provide PowerShell scripts in `scripts/` to standardize launching each process with its own virtualenv and explicit env vars, reducing operator error in production.
- Test pyramid is explicit: pure unit tests for strategy/risk/settings, integration tests for SQLite + run-loop wiring, and e2e smoke tests per process role to verify safety boundaries.
