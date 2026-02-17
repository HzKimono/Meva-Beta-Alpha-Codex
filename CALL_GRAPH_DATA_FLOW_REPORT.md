# CALL GRAPH & DATA FLOW REPORT

## 1) Main loop lifecycle: startup -> steady state -> shutdown

### 1.1 Stage 3 lifecycle (`run`)
**Primary entrypoint chain**
1. `src/btcbot/__main__.py::main` hands off to `src/btcbot/cli.py::main`.
2. `src/btcbot/cli.py::main` parses subcommands (`run`, `stage4-run`, `stage7-run`, etc.) and loads settings via `_load_settings`.
3. For `run`, `main` calls `src/btcbot/cli.py::run_cycle` directly or via `run_with_optional_loop`.
4. `run_cycle` composes dependencies: `build_exchange_stage3`, `StateStore`, `PortfolioService`, `MarketDataService`, `SweepService`, `ExecutionService`, `AccountingService`, `StrategyService`, `RiskService`.
5. `run_cycle` executes a single planning/execution pass and finally calls `flush_instrumentation`, `_flush_logging_handlers`, `_close_best_effort`.

**Stage 3 call graph (single cycle)**
```text
python -m btcbot
  -> btcbot.__main__.main()
    -> btcbot.cli.main()
      -> _load_settings()
      -> run_with_optional_loop(run_cycle)
        -> run_cycle(settings)
          -> build_exchange_stage3(settings)                    [SIDE EFFECT: NET client init]
          -> StateStore(db_path)                                [SIDE EFFECT: DB schema ensure]
          -> PortfolioService.get_balances()                    [SIDE EFFECT: NET private API]
          -> MarketDataService.get_best_bids()
             -> MarketDataService.get_best_bid_ask()
                -> ExchangeClient.get_orderbook()               [SIDE EFFECT: NET public API]
          -> StartupRecoveryService.run()
             -> ExecutionService.refresh_order_lifecycle()      [SIDE EFFECT: NET + DB]
             -> AccountingService.refresh()                     [SIDE EFFECT: NET + DB]
          -> ExecutionService.cancel_stale_orders()             [SIDE EFFECT: NET + DB]
          -> AccountingService.refresh()                        [SIDE EFFECT: NET + DB]
          -> StrategyService.generate()
             -> ProfitAwareStrategyV1.generate_intents()
          -> RiskService.filter()
             -> RiskPolicy.evaluate()
          -> ExecutionService.execute_intents()                 [SIDE EFFECT: NET + DB]
          -> StateStore.set_last_cycle_id()                     [SIDE EFFECT: DB]
          -> flush_instrumentation() / _close_best_effort()
```

### 1.2 Stage 4 lifecycle (`stage4-run`)
- `src/btcbot/cli.py::run_cycle_stage4` enforces live policy gates, resolves DB path, and delegates to `src/btcbot/services/stage4_cycle_runner.py::Stage4CycleRunner.run_one_cycle`.
- `Stage4CycleRunner.run_one_cycle` builds Stage4 service graph: `ExchangeRulesService`, Stage4 `AccountingService`, `OrderLifecycleService`, `ReconcileService`, `RiskPolicy`, `RiskBudgetService`, Stage4 `ExecutionService`, `DecisionPipelineService`, `LedgerService`, etc.
- If `dynamic_universe_enabled`, it calls `DynamicUniverseService.select` before decision generation.

### 1.3 Stage 7 lifecycle (`stage7-run`)
- `src/btcbot/cli.py::run_cycle_stage7` enforces `--dry-run` + `STAGE7_ENABLED`, then delegates to `src/btcbot/services/stage7_cycle_runner.py::Stage7CycleRunner.run_one_cycle`.
- `Stage7CycleRunner.run_one_cycle` composes Stage7 metrics/ledger/risk-budget/adaptation flows and records cycle metrics.

### 1.4 Shutdown behavior
- Stage 3 explicitly calls `flush_instrumentation`, `_flush_logging_handlers`, and `_close_best_effort(exchange, "exchange")` in `run_cycle` `finally` block.
- Stage4/Stage7 cleanup is orchestrator-specific inside runner implementations; explicit finalization depth is **UNKNOWN** without full tracing of each runner path. Confirm by tracing all return/exception branches in `src/btcbot/services/stage4_cycle_runner.py` and `src/btcbot/services/stage7_cycle_runner.py`.

---

## 2) Market data ingestion flow (source -> parsing -> normalization -> caching)

### 2.1 Stage 3 market data path
```text
BTCTurk REST /api/v2/orderbook [NET]
  -> BtcturkHttpClient._get(path="/api/v2/orderbook")
  -> BtcturkHttpClient.get_orderbook(symbol)
     -> _parse_best_price(bids/asks)
  -> MarketDataService.get_best_bid_ask(symbol)
  -> MarketDataService.get_best_bids(symbols)
  -> cli.run_cycle(...) mark_prices map
     normalize_symbol(symbol)
```

### 2.2 Exchange metadata/rules caching path
```text
BTCTurk REST /api/v2/server/exchangeinfo [NET]
  -> BtcturkHttpClient.get_exchange_info()
  -> MarketDataService._refresh_symbol_rules_cache()
     -> pair_info_to_symbol_rules(pair)
     -> normalize_symbol(pair.pair_symbol)
     -> _rules_cache[symbol] = SymbolRules
  -> MarketDataService.get_symbol_rules(pair_symbol)
     -> TTL check via _cache_expired()
```

### 2.3 Async market data boundary (optional websocket)
- Async ingestion entrypoint: `src/btcbot/adapters/btcturk/ws_client.py::BtcturkWsClient.run`.
- Async boundaries:
  - `connect_fn(url)` (**await**), `_read_loop` (**await socket.recv**), `_dispatch_loop` (**await queue.get**), optional `_heartbeat_loop` (**await sleep/send**).
- Queue buffering: `asyncio.Queue[WsEnvelope]` with `queue_maxsize`; overflow increments `ws_backpressure_drops`.

### 2.4 Replay data path (backtest/simulation)
- `src/btcbot/services/market_data_replay.py` + `src/btcbot/adapters/replay_exchange.py` are used in replay flows.
- Dataset schema enforcement is in `src/btcbot/replay/validate.py`.
- Side effects: filesystem reads from replay dataset (`data/replay` or custom path).

---

## 3) Strategy/agent decision pipeline (inputs -> features -> decision -> risk gate -> order intent)

### 3.1 Stage 3 strategy pipeline
```text
Inputs:
  balances (PortfolioService.get_balances)          [NET private API]
  bid/ask (MarketDataService.get_best_bid_ask)      [NET public API]
  positions (AccountingService.get_positions)        [DB read]
  open orders (StateStore.find_open_or_unknown_orders) [DB read]

Strategy:
  StrategyService.generate()
    -> build StrategyContext
    -> ProfitAwareStrategyV1.generate_intents()
       - feature: spread=(ask-bid)/bid
       - feature: position.qty, position.avg_cost
       - threshold: min_profit_bps
       - output: list[btcbot.domain.intent.Intent]

Risk gate:
  RiskService.filter()
    -> RiskPolicy.evaluate()
       - checks: max orders/cycle, max open orders/symbol,
                 cooldown_seconds,
                 min_notional/price/qty quantization,
                 notional cap,
                 investable TRY limit

Execution input:
  approved intents -> ExecutionService.execute_intents()
```

### 3.2 Stage4/Stage7 decision pipeline
- Stage4 uses `DecisionPipelineService.run_cycle` with universe selection, allocation, and action->order mapping (`src/btcbot/services/decision_pipeline_service.py`).
- Stage4 can include agent policy flow in `Stage4CycleRunner` via:
  - `RuleBasedPolicy` / `LlmPolicy` / `FallbackPolicy` (`src/btcbot/agent/policy.py`)
  - guardrails `SafetyGuard.apply` (`src/btcbot/agent/guardrails.py`)
  - audit writes through `AgentAuditTrail` (`src/btcbot/agent/audit.py`) [DB side effect].

### 3.3 Agent decision shape
- Agent contracts: `AgentContext`, `AgentDecision`, `SafeDecision` in `src/btcbot/agent/contracts.py`.
- Guardrail outputs can force observe-only action and strip unsafe intents (`SafetyGuard.apply`).

---

## 4) Execution pipeline (intent -> order placement -> ack -> fill events -> reconciliation)

### 4.1 Core Stage 3 execution path
```text
approved intents
  -> ExecutionService.execute_intents(intents, cycle_id)
      -> refresh_order_lifecycle(symbols)
         -> exchange.get_open_orders(symbol)            [NET]
         -> exchange.get_all_orders(...)                [NET]
         -> state_store.update_order_status(...)        [DB write]

      -> record_action(action_type, payload_hash)       [DB write dedupe]
      -> if dry_run: attach_action_metadata(...)        [DB write]
      -> else:
           market_data_service.get_symbol_rules()       [NET if cache miss]
           validate_order(...)
           exchange.place_limit_order(...)              [NET private order submit]
           state_store.save_order(...)                  [DB write]
           state_store.attach_action_metadata(...)      [DB write]

      -> on uncertain exception:
           _reconcile_submit(...)
             -> get_open_orders/get_all_orders          [NET]
             -> match by client_order_id/fallback fields
             -> set reconcile status metadata           [DB write]
```

### 4.2 Cancel/stale order path
```text
ExecutionService.cancel_stale_orders(cycle_id)
  -> exchange.list_open_orders()                        [NET]
  -> TTL check vs order.created_at
  -> record_action("cancel_order"|"would_cancel_order") [DB]
  -> exchange.cancel_order(order_id)                    [NET private cancel]
  -> on uncertain exception: _reconcile_cancel(order)
       -> get_open_orders + get_all_orders              [NET]
  -> state_store.update_order_status(...)               [DB write]
  -> state_store.attach_action_metadata(...)            [DB write]
```

### 4.3 Fill ingestion and accounting path
```text
AccountingService.refresh(symbols, mark_prices)
  -> exchange.get_recent_fills(symbol)                  [NET private fills]
  -> state_store.save_fill(fill)                        [DB write idempotent]
  -> AccountingService._apply_fill(fill)
     -> state_store.get_position(symbol)                [DB read]
     -> state_store.save_position(position)             [DB write]
  -> recompute unrealized pnl from mark_prices
```

---

## 5) State transitions (orders/positions/balances) as a state machine (ASCII)

### 5.1 Order lifecycle state machine (Stage3 model)
States come from `src/btcbot/domain/models.py::OrderStatus` and exchange snapshots (`ExchangeOrderStatus`).

```text
                      +-------------------+
                      |  NEW INTENT       |
                      +---------+---------+
                                |
                                v
                        record_action dedupe [DB]
                                |
        +-----------------------+------------------------+
        |                                                |
        v                                                v
 +--------------+                                  +--------------+
 | DRY_RUN PATH |                                  | LIVE PATH    |
 | would_place  |                                  | place_limit  |
 +------+-------+                                  +------+-------+
        |                                                 |
        v                                                 v
 action metadata [DB]                               submit ACK [NET]
        |                                                 |
        v                                                 v
   (no exchange order)                             save_order [DB]
                                                          |
                                                          v
                                               OPEN / PARTIAL / FILLED / CANCELED
                                                          |
                                                          v
                                            refresh_order_lifecycle + reconcile
                                            (openOrders/allOrders) [NET]
                                                          |
                                                          v
                                           update_order_status [DB]
                                                          |
                                                          v
                                           UNKNOWN (uncertain error unresolved)
```

### 5.2 Position lifecycle (Stage3 accounting)
```text
No Position
   |
   | BUY fill
   v
Long Position(qty>0, avg_cost updated)
   |
   | SELL partial fill
   v
Long Position(qty reduced, realized_pnl += ...)
   |
   | SELL to zero
   v
Flat Position(qty=0, avg_cost=0)
```

### 5.3 Balance flow
- Source of balances: `PortfolioService.get_balances` (exchange private API, fallback dry-run behavior handled by upstream service/adapter paths).
- Decision usage: cash TRY determines `investable_try` (`cli.run_cycle`).
- Accounting updates positions/pnl in DB; account balances themselves are fetched from exchange each cycle.

---

## 6) Concurrency model: asyncio/tasks/threads/queues, shared state and locks

### 6.1 Concurrency summary
- Stage3/Stage4/Stage7 runners are primarily synchronous control flows from CLI orchestrators.
- Async components are isolated in BTCTurk WS/async adapter modules.
- No explicit thread pool orchestration was found in main runtime orchestration paths.

### 6.2 Async boundaries (explicit)
1. `BtcturkWsClient.run` event loop tasks (`_read_loop`, `_dispatch_loop`, optional `_heartbeat_loop`) in `src/btcbot/adapters/btcturk/ws_client.py`.
2. `BtcturkWsClient._read_loop` awaits socket receive with timeout and pushes envelopes to `asyncio.Queue`.
3. `BtcturkWsClient._dispatch_loop` consumes queue and awaits per-channel handlers.
4. `src/btcbot/adapters/btcturk/rest_client.py` defines async request methods around `httpx.AsyncClient` (**async network boundary**).

### 6.3 Shared state and lock boundaries
- Persistent shared state: SQLite via `StateStore` (`src/btcbot/services/state_store.py`), with WAL mode, busy timeout, and explicit `transaction()` helper.
- Cross-process exclusion: `single_instance_lock` (`src/btcbot/services/process_lock.py`) file lock keyed by DB path/account key.
- In-memory shared async queue: `BtcturkWsClient.queue` (`asyncio.Queue`) between reader and dispatcher tasks.

### 6.4 Side-effect matrix (explicit)
- **Network (REST/WS)**:
  - `BtcturkHttpClient._get`, `_private_request`, `_private_get`, `place_limit_order`, `cancel_order`, `get_open_orders`, `get_all_orders`, `get_recent_fills`.
  - `BtcturkWsClient` socket connect/send/recv.
- **DB (SQLite)**:
  - `StateStore` initialization and all CRUD methods (`record_action`, `save_fill`, `save_position`, `update_order_status`, etc.).
- **Disk/filesystem**:
  - SQLite file writes (`STATE_DB_PATH`).
  - Replay dataset read/write (`btcbot.replay.tools`, `btcbot.replay.validate`).
  - Process lock file + PID file (`process_lock.py`).

### 6.5 Known uncertainty markers
- **UNKNOWN**: whether any runner-internal helper starts additional background threads/tasks beyond the visible async adapter boundaries in normal Stage4/Stage7 execution. Confirm by tracing every called service in `stage4_cycle_runner.py` and `stage7_cycle_runner.py` transitively.
