# End-to-End Execution Flow Narrative (Evidence-Based)

Scope: this report documents the runtime behavior from repository evidence only, focused on the default `run` command flow and related runtime components.

## 1) Happy-path runtime flow

### Step 1 — Startup and command dispatch
- **Files/functions**
  - `src/btcbot/__main__.py` → module entrypoint to CLI.
  - `src/btcbot/cli.py::main()` parses subcommands, bootstraps settings/logging/instrumentation, then dispatches `run` / `stage4-run` / `stage7-run` etc.
- **Inputs**
  - CLI args (`run`, `--loop`, `--dry-run`, `--env-file`, etc.).
  - Environment variables and optional dotenv path.
- **Outputs**
  - Exit code and command routing to cycle functions.
- **Side effects**
  - Logging/instrumentation setup.
  - Optional loop scheduler starts (`run_with_optional_loop`).

### Step 2 — Configuration load and safety validation
- **Files/functions**
  - `src/btcbot/cli.py::_load_settings()`.
  - `src/btcbot/config.py::Settings` (Pydantic settings schema with env aliases and defaults).
  - `src/btcbot/security/secrets.py` helpers invoked by `_load_settings` (`build_default_provider`, `inject_runtime_secrets`, `validate_secret_controls`, `log_secret_validation`).
- **Inputs**
  - `.env.live` (default in `Settings`) or `--env-file` override.
  - process env vars (`BTCTURK_*`, `DRY_RUN`, `KILL_SWITCH`, `LIVE_TRADING*`, etc.).
- **Outputs**
  - `Settings` instance used across runtime.
- **Side effects**
  - Secret-injection into runtime env for selected keys.
  - Secret-control validation logs; failure raises `ValueError`.

### Step 3 — Client and service initialization
- **Files/functions**
  - `src/btcbot/cli.py::run_cycle()`.
  - `src/btcbot/services/exchange_factory.py::build_exchange_stage3()`.
  - `src/btcbot/services/state_store.py::StateStore.__init__()`.
- **Inputs**
  - Effective safety policy (`DRY_RUN`, `KILL_SWITCH`, live arming flags, `SAFE_MODE`).
  - DB path (`STATE_DB_PATH`).
- **Outputs**
  - Exchange client (dry-run wrapper or live `BtcturkHttpClient`).
  - SQLite-backed `StateStore`.
  - Service graph: `PortfolioService`, `MarketDataService`, `ExecutionService`, `AccountingService`, `StrategyService`, `RiskService`, `SweepService`.
- **Side effects**
  - `StateStore` opens SQLite and creates/ensures schema/tables.
  - In dry-run exchange build, startup fetches public exchange info/orderbooks best-effort.

### Step 4 — Startup recovery before trading actions
- **Files/functions**
  - `src/btcbot/cli.py::run_cycle()` calls `StartupRecoveryService().run(...)`.
  - `src/btcbot/services/startup_recovery.py::StartupRecoveryService.run()`.
- **Inputs**
  - Symbols, mark prices, services (`execution_service`, `accounting_service`, `portfolio_service`).
- **Outputs**
  - `StartupRecoveryResult` with `observe_only_required`, reason, invariants, fills inserted.
- **Side effects**
  - Refresh order lifecycle (`execution_service.refresh_order_lifecycle`).
  - Refresh/apply fills through accounting if mark prices available.
  - Balance/position invariants checked; logs can force observe-only mode for cycle.

### Step 5 — Market data ingest and portfolio snapshot
- **Files/functions**
  - `src/btcbot/services/portfolio_service.py::get_balances()`.
  - `src/btcbot/services/market_data_service.py::get_best_bids()` and `get_best_bid_ask()`.
  - `src/btcbot/adapters/btcturk_http.py::get_balances()`, `get_orderbook()`, `get_exchange_info()`.
- **Inputs**
  - Symbol list from settings.
- **Outputs**
  - Free/locked balances.
  - Best bid/ask per symbol.
- **Side effects**
  - Network calls to BTCTurk endpoints (public and private depending on method).
  - Metrics (`stale_market_data_rate`, reconcile latency histograms) emitted in run cycle.

### Step 6 — Signal/strategy generation
- **Files/functions**
  - `src/btcbot/services/strategy_service.py::generate()`.
  - Strategy implementation wired in `run_cycle`: `btcbot.strategies.profit_v1.ProfitAwareStrategyV1`.
- **Inputs**
  - Cycle ID, symbols, balances.
  - Orderbooks from market data, positions from accounting, open/unknown order counts from state store.
- **Outputs**
  - Raw intent list (`Intent`).
- **Side effects**
  - No network side effects in strategy service itself; reads state store/accounting/market snapshots.

### Step 7 — Risk checks/filtering
- **Files/functions**
  - `src/btcbot/services/risk_service.py::filter()`.
  - `src/btcbot/risk/policy.py::RiskPolicy.evaluate()`.
- **Inputs**
  - Raw intents, cycle context, open orders, prior intent timestamps, TRY cash/investable budget.
- **Outputs**
  - Approved intents.
- **Side effects**
  - Risk block events logged with rule reason.
  - Approved intents recorded in state store (`record_intent`).

### Step 8 — Order placement and tracking
- **Files/functions**
  - `src/btcbot/services/execution_service.py::execute_intents()`.
  - `src/btcbot/services/execution_service.py::refresh_order_lifecycle()`, `cancel_stale_orders()`.
  - Exchange adapter methods: `place_limit_order`, `cancel_order`, `get_open_orders`, `get_all_orders`, `get_order`.
- **Inputs**
  - Approved intents + cycle_id.
  - Current open orders and idempotency state from SQLite.
- **Outputs**
  - Count of placed/simulated orders.
  - Updated order statuses/idempotency records.
- **Side effects**
  - DB writes: action records, idempotency reservations/finalization, order metadata/status updates.
  - Live mode network writes: BTCTurk private POST/DELETE order endpoints.
  - Dry-run path records simulated actions and skips exchange write calls.

### Step 9 — PnL/accounting refresh
- **Files/functions**
  - `src/btcbot/accounting/accounting_service.py::refresh()` and `_apply_fill()`.
- **Inputs**
  - Symbols + mark prices.
  - Recent fills from exchange client (`get_recent_fills` if available).
- **Outputs**
  - Number of newly inserted fills.
  - Updated positions (qty, avg_cost, realized/unrealized PnL, fees).
- **Side effects**
  - DB writes to fills/positions via `StateStore` methods.

### Step 10 — Cycle completion and shutdown/restart behavior
- **Files/functions**
  - `src/btcbot/cli.py::run_cycle()` finally block.
  - `src/btcbot/cli.py::run_with_optional_loop()` for repeated scheduling.
  - `src/btcbot/observability.py` flush/shutdown helpers.
- **Inputs**
  - Loop options (`--loop`, cycle seconds, jitter, max cycles).
- **Outputs**
  - Return code per cycle/command.
- **Side effects**
  - Flush instrumentation and log handlers each cycle exit path.
  - Close exchange client best effort.
  - Optional sleep/retry loop for next cycle.

---

## 2) Per-step I/O and side-effects matrix

| Step | File(s) / Function(s) | Inputs | Outputs | Side effects |
|---|---|---|---|---|
| 1. Startup dispatch | `cli.main` | CLI args | Selected command function + exit code | Logging + instrumentation setup |
| 2. Config load | `cli._load_settings`, `config.Settings` | Env, `.env.live` / `--env-file` | `Settings` object | Secret injection/validation logs |
| 3. Init clients/services | `build_exchange_stage3`, `StateStore.__init__`, `run_cycle` service constructors | Settings, DB path | Exchange + state + services | SQLite schema init; dry-run public data fetch |
| 4. Startup recovery | `StartupRecoveryService.run` | cycle_id, symbols, mark_prices, services | Recovery result | Lifecycle refresh; accounting refresh; invariant logs |
| 5. Market/portfolio ingest | `PortfolioService.get_balances`, `MarketDataService.get_best_bids`, `BtcturkHttpClient.get_*` | Symbols | balances + bid/ask snapshot | REST network calls |
| 6. Strategy | `StrategyService.generate` | balances + orderbooks + positions + open orders | raw intents | Reads from state/accounting services |
| 7. Risk | `RiskService.filter`, `RiskPolicy.evaluate` | raw intents + risk context | approved intents | Record approved intents to DB |
| 8. Execution | `ExecutionService.execute_intents` (+ refresh/cancel) | approved intents, cycle id | placed/simulated count | DB action/idempotency/order writes; optional live submit/cancel network calls |
| 9. Accounting | `AccountingService.refresh` | symbols, mark prices, fills | inserted count + position updates | DB fill/position updates |
| 10. End cycle | `run_cycle` finally + `run_with_optional_loop` | command/loop params | rc + next iteration decision | flush metrics/logs, close clients, sleep/retry |

---

## 3) Concurrency model

### Observed runtime model (default `run` path)
- Single process, synchronous command execution in `cli.main` + `run_cycle`.
- Optional repeated scheduling implemented as an in-process loop (`run_with_optional_loop`) with sleep/jitter and retry-on-exception.
- Single-instance protection for stage4/stage7 commands via file lock context manager (`single_instance_lock`).

### Async / queue components present in repo
- `src/btcbot/adapters/btcturk/ws_client.py` defines an **asyncio** WebSocket client with:
  - `asyncio.Queue` for envelopes,
  - background tasks (`_read_loop`, `_dispatch_loop`, optional `_heartbeat_loop`),
  - reconnect/backoff flow.
- This async WS client is present but direct invocation from `cli.run` happy path is not evidenced in inspected files.

### Shared state and protection
- Persistent shared state: SQLite (`StateStore`) with WAL mode, busy timeout, and explicit transaction context (`BEGIN IMMEDIATE`).
- In-process observability singleton protected by `threading.Lock` in `configure_instrumentation`.
- Cross-process mutual exclusion (selected commands) via OS file lock (`single_instance_lock`).

### Unknowns (explicit)
- Whether websocket client is used by any production entrypoint outside inspected CLI paths is **unknown** from inspected files.
- No explicit thread pool / multiprocessing orchestration was found in inspected runtime entrypoints.

---

## A) Sequence diagram (text, numbered)

1. Operator invokes `btcbot run ...` (or `python -m btcbot.cli run ...`).
2. `__main__` forwards to `cli.main`.
3. `cli.main` parses CLI arguments/subcommand.
4. `cli._load_settings` builds provider, injects runtime secrets, loads `Settings`, validates secret controls.
5. Logging and instrumentation are configured.
6. Effective universe is resolved and side-effects arm state is printed.
7. For `run`, optional scheduler enters `run_with_optional_loop` (or single cycle).
8. `run_cycle` computes live policy, enforces arming gates, builds exchange and `StateStore`.
9. `run_cycle` constructs services (portfolio, market, execution, accounting, strategy, risk, sweep).
10. Balances + best bids fetched; startup mark prices formed.
11. `StartupRecoveryService.run` executes lifecycle refresh + accounting refresh + invariants.
12. If recovery requires observe-only, execution service gate flags are tightened.
13. Runtime lifecycle refresh and stale-cancel checks run (`ExecutionService`).
14. Fresh market bids and mark prices are computed.
15. `AccountingService.refresh` ingests recent fills and updates positions/unrealized PnL.
16. `StrategyService.generate` emits raw intents.
17. `RiskService.filter` + `RiskPolicy.evaluate` produce approved intents.
18. `ExecutionService.execute_intents` runs idempotency flow and either simulates or performs submit calls.
19. Cycle summary is logged; last cycle id persisted.
20. `finally`: flush instrumentation/log handlers and close exchange.
21. If loop mode enabled, sleep/jitter and continue next cycle; otherwise return exit code.

---

## B) Critical call graph (top 15 functions/classes)

1. `btcbot.cli.main`
2. `btcbot.cli._load_settings`
3. `btcbot.cli.run_with_optional_loop`
4. `btcbot.cli.run_cycle`
5. `btcbot.services.exchange_factory.build_exchange_stage3`
6. `btcbot.services.state_store.StateStore`
7. `btcbot.services.startup_recovery.StartupRecoveryService.run`
8. `btcbot.services.market_data_service.MarketDataService.get_best_bids`
9. `btcbot.services.portfolio_service.PortfolioService.get_balances`
10. `btcbot.accounting.accounting_service.AccountingService.refresh`
11. `btcbot.services.strategy_service.StrategyService.generate`
12. `btcbot.services.risk_service.RiskService.filter`
13. `btcbot.risk.policy.RiskPolicy.evaluate`
14. `btcbot.services.execution_service.ExecutionService.execute_intents`
15. `btcbot.adapters.btcturk_http.BtcturkHttpClient` (notably `get_orderbook`, `get_balances`, `submit_limit_order`, `cancel_order_by_client_order_id`, `_private_request`)

---

## C) State model (in-memory vs persisted)

### In-memory state
- CLI/runtime ephemeral values: args, run_id, cycle_id, policy decisions.
- Service object graph and temporary snapshots (balances, bids, mark prices, intents, approved intents).
- MarketDataService symbol-rules cache (`_rules_cache` and timestamp).
- ExecutionService runtime flags (dry_run, kill_switch, safe_mode) and unknown-order probe configuration.
- Observability singleton and metric instrument handles.
- (WS component) async queue/tasks/stop event in `BtcturkWsClient` when used.

### Persisted state (SQLite via `StateStore`)
- Core tables initialized in `_init_db`: `actions`, `orders`, `fills`, `positions`, `intents`, `meta`, plus idempotency and stage-specific tables.
- Idempotency lifecycle for submit/cancel actions.
- Order lifecycle/status reconciliation metadata.
- Accounting persistence: fills and position snapshots.
- Stage7 persistence families (run metrics, cycle trace, params, risk decisions, etc.).

### State protection / consistency controls
- SQLite WAL + busy timeout on connections.
- Transaction context manager with `BEGIN IMMEDIATE` for atomic units.
- Cross-process single-instance lock for stage4/stage7 command paths.

---

## Explicit unknowns / missing evidence

1. Concrete websocket transport library used at runtime is not identifiable from inspected dependency declarations and CLI happy-path wiring.
2. A default `run`-path consumer that starts `BtcturkWsClient` is not evidenced in inspected files.
3. External orchestrators beyond Docker Compose/CLI (e.g., systemd/K8s manifests) are not present in inspected repository files.
