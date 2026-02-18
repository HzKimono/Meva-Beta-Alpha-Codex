# End-to-End Execution Flow Narrative (evidence-only)

Scope: Stage 3 default runtime (`btcbot run`) with supporting Stage4/Stage7 concurrency notes where directly evidenced.

## A) Sequence diagram in text (happy path)

1) **Process starts / CLI parse**
   - `python -m btcbot` -> `btcbot.cli:main`.
   - `main()` builds subcommands and parses args.

2) **Configuration + instrumentation bootstrap**
   - `_load_settings()` resolves env file, injects runtime secrets, loads `Settings`, validates secret controls.
   - `setup_logging()` and `configure_instrumentation()` are called.
   - Effective universe is applied via `_apply_effective_universe()`.

3) **Scheduler decision**
   - For `run`, `run_with_optional_loop()` either executes once or loops with sleep/jitter and retry-on-exception behavior.

4) **Cycle begins (`run_cycle`)**
   - Live-write policy is computed (`_compute_live_policy`), arm checks logged, unsafe live mode rejected.
   - Exchange client is built (`build_exchange_stage3`), `StateStore` initialized, cycle IDs created.

5) **Service graph initialization**
   - `PortfolioService`, `MarketDataService`, `SweepService`, `ExecutionService`, `AccountingService`, `StrategyService`, `RiskService` are constructed.

6) **Market/account data ingest**
   - `PortfolioService.get_balances()` reads balances from exchange.
   - `MarketDataService.get_best_bids()` pulls orderbook bids for configured symbols.

7) **Startup recovery / restart handling**
   - `StartupRecoveryService.run()` refreshes order lifecycle, optionally refreshes accounting fills, and enforces invariants (negative balances/positions trigger observe-only behavior).

8) **Order lifecycle reconcile phase**
   - `ExecutionService.cancel_stale_orders()` is called before new submissions.

9) **PnL/accounting refresh**
   - `AccountingService.refresh()` fetches recent fills, writes fills/positions, recalculates unrealized PnL with mark prices.

10) **Signal/strategy generation**
    - `StrategyService.generate()` builds `StrategyContext` from symbols/orderbooks/positions/balances/open-orders and calls strategy (`ProfitAwareStrategyV1`) to emit intents.

11) **Risk checks**
    - `RiskService.filter()` builds `RiskPolicyContext` (open-order counts, cooldown timestamps, cash/investable amounts), evaluates `RiskPolicy`, and records approved intents.

12) **Order placement path**
    - `ExecutionService.execute_intents()`:
      - refreshes lifecycle + prunes idempotency keys,
      - short-circuits in safe mode/kill-switch,
      - reserves idempotency keys,
      - validates symbol rules/quantization,
      - submits `exchange.place_limit_order(...)` in live mode (or records dry-run actions),
      - persists orders/action metadata/idempotency finalization.

13) **Cycle completion persistence + telemetry**
    - Last cycle ID is persisted (`state_store.set_last_cycle_id`), cycle summary logged, instrumentation metrics emitted.

14) **Shutdown path (per cycle and process interruption)**
    - In `run_cycle` finally: `flush_instrumentation()`, `_flush_logging_handlers()`, `_close_best_effort(exchange)`.
    - In loop mode, `KeyboardInterrupt` exits gracefully with logs.

---

## Per-step IO + side effects matrix

### 1) Startup / config
- **Files/functions**: `src/btcbot/__main__.py`, `src/btcbot/cli.py::main`, `src/btcbot/cli.py::_load_settings`, `src/btcbot/config.py::Settings`.
- **Inputs**: CLI args, env vars, optional dotenv path.
- **Outputs**: `Settings` object, configured logger/instrumentation.
- **Side effects**: Secret provider reads env/dotenv; startup validation logs.

### 2) Client init
- **Files/functions**: `src/btcbot/services/exchange_factory.py::build_exchange_stage3`.
- **Inputs**: settings (dry-run, API keys, symbols, base URL).
- **Outputs**: `ExchangeClient` implementation (dry-run wrapper or BTCTurk HTTP client).
- **Side effects**: In dry-run construction, public HTTP calls may fetch exchange info/orderbooks.

### 3) Market data ingest
- **Files/functions**: `src/btcbot/services/portfolio_service.py::get_balances`, `src/btcbot/services/market_data_service.py::get_best_bids/get_best_bid_ask`.
- **Inputs**: symbol list.
- **Outputs**: balances list, symbol->bid map.
- **Side effects**: Exchange network reads (balances/orderbook).

### 4) Startup recovery
- **Files/functions**: `src/btcbot/services/startup_recovery.py::StartupRecoveryService.run`.
- **Inputs**: cycle_id, symbols, execution/accounting/portfolio services, mark prices.
- **Outputs**: `StartupRecoveryResult` (observe-only flags, fills count, invariant errors).
- **Side effects**: refreshes order lifecycle, may write accounting state, logs invariant failures.

### 5) Strategy
- **Files/functions**: `src/btcbot/services/strategy_service.py::generate`.
- **Inputs**: cycle_id, symbols, balances, current positions/open-orders/orderbooks/settings.
- **Outputs**: list of `Intent`.
- **Side effects**: none directly (reads state/services).

### 6) Risk
- **Files/functions**: `src/btcbot/services/risk_service.py::filter`, `btcbot.risk.policy.RiskPolicy.evaluate`.
- **Inputs**: intents, open-order state, last intent timestamps, TRY cash/investable targets.
- **Outputs**: approved intents.
- **Side effects**: records approved intents in state store.

### 7) Order placement / tracking
- **Files/functions**: `src/btcbot/services/execution_service.py::execute_intents`, `refresh_order_lifecycle`, `cancel_stale_orders`.
- **Inputs**: intents/order-intents, cycle_id, exchange+state store, safety gates.
- **Outputs**: count of placed orders.
- **Side effects**:
  - Network: open orders/all orders lookups, submit/cancel requests.
  - DB: idempotency reservations/finalization, actions, order rows/status, metadata updates.
  - In-memory: kill-switch/safe mode runtime flags, temporary intent normalization.

### 8) PnL/accounting
- **Files/functions**: `src/btcbot/accounting/accounting_service.py::refresh/_apply_fill/compute_total_pnl`.
- **Inputs**: symbols, mark prices, recent fills from exchange.
- **Outputs**: inserted fill count, updated positions and PnL fields.
- **Side effects**: DB writes for fills and positions; logs when fee currency is unexpected.

### 9) Shutdown/restart
- **Files/functions**: `src/btcbot/cli.py::run_cycle` finally block, `run_with_optional_loop`, `src/btcbot/services/startup_recovery.py`.
- **Inputs**: process signals/errors.
- **Outputs**: exit code and graceful stop logs.
- **Side effects**: flush telemetry/log handlers, close exchange resources; restart recovery runs on next cycle startup.

---

## 3) Concurrency model

### Observed model (Stage 3 happy path)
- **Primary execution model**: synchronous single-process loop in `run_with_optional_loop`; no worker threads/process pool in the Stage 3 path.
- **Scheduler**: while-loop with sleep + optional jitter + bounded retry per cycle.

### Async/queue components present in repository
- `BtcturkWsClient` is asyncio-based with tasks (`_read_loop`, `_dispatch_loop`, optional `_heartbeat_loop`) and an internal `asyncio.Queue`.
- `BtcturkRestClient` is async (`httpx.AsyncClient`), with retry and async token bucket acquisition.

### Shared state and protection
- **Persisted shared state**: SQLite via `StateStore`.
- **DB protection mechanisms**:
  - SQLite WAL + busy timeout.
  - Explicit transaction context uses `BEGIN IMMEDIATE`.
- **Single-instance protection**:
  - `single_instance_lock` file lock used in `stage4-run` and `stage7-run` entrypoints.
  - Stage 3 `run` path does **not** show the same lock in `run_cycle`.

### Unknown/unclear items (explicit)
- Whether websocket async client is wired into Stage 3 production path is not explicit in `run_cycle`; Stage 3 directly uses REST-style service calls in the observed flow.
- Thread-level concurrency beyond asyncio tasks in WS module is not evidenced in inspected files.

---

## B) Critical call graph (top 15 functions/classes)

1. `btcbot.__main__:main` -> delegates to `btcbot.cli:main`
2. `btcbot.cli:main`
3. `btcbot.cli:_load_settings`
4. `btcbot.cli:run_with_optional_loop`
5. `btcbot.cli:run_cycle`
6. `btcbot.services.exchange_factory:build_exchange_stage3`
7. `btcbot.services.state_store:StateStore`
8. `btcbot.services.portfolio_service:PortfolioService.get_balances`
9. `btcbot.services.market_data_service:MarketDataService.get_best_bids`
10. `btcbot.services.startup_recovery:StartupRecoveryService.run`
11. `btcbot.accounting.accounting_service:AccountingService.refresh`
12. `btcbot.services.strategy_service:StrategyService.generate`
13. `btcbot.services.risk_service:RiskService.filter`
14. `btcbot.services.execution_service:ExecutionService.execute_intents`
15. `btcbot.adapters.btcturk_http:BtcturkHttpClient.place_limit_order` (live side effect endpoint call)

---

## C) State model (memory vs persisted)

### In-memory state
- CLI/runtime context: `run_id`, `cycle_id`, computed policy flags.
- Service instances per cycle: portfolio/market/accounting/strategy/risk/execution services.
- Transient market/account/accounting data: balances, bids, mark prices, raw/approved intents.
- `MarketDataService` rules cache (`_rules_cache`, `_rules_cache_loaded_at`).
- `ExecutionService` runtime flags (`dry_run`, `kill_switch`, `safe_mode`) and recovery counters (used during lifecycle handling).
- Async WS client queue/tasks (if that path is used).

### Persisted state (SQLite via `StateStore`)
- Core tables include: `actions`, `orders`, `fills`, `positions`, `intents`, `meta`.
- Additional persisted domains include risk/anomaly/stage4/stage7/idempotency/ledger/cycle metrics tables.
- Idempotency keys are persisted and pruned/recovered; order/action metadata reconciliation statuses are persisted.

### Persistence semantics visible in code
- DB connections use WAL and busy timeout; transactions can be explicitly wrapped with `BEGIN IMMEDIATE`.
- Cycle metadata includes `last_cycle_id` update and action/audit records through state store APIs.
