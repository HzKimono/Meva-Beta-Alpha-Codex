# Critical Path Call-Graph Verification (Grounded)

Scope: verified from code paths only; no behavior claims without source evidence.

## 1) Runtime entrypoints (first executable unit)

- **Console script: `btcbot`** → first unit `btcbot.cli:main` (configured in project scripts). (evidence: `pyproject.toml` L20-L22)
- **Module entrypoint: `python -m btcbot`** → first unit `btcbot.__main__` then `main()`. (evidence: `src/btcbot/__main__.py` L3-L6)
- **Module entrypoint: `python -m btcbot.cli`** → first unit `btcbot.cli.main` via module guard. (evidence: `src/btcbot/cli.py` L63-L74, L1099-L1100)
- **Utility script: exchange info smoke** → first unit `fetch_exchangeinfo()` from `check_exchangeinfo.py` module guard. (evidence: `check_exchangeinfo.py` L8-L20)
- **Utility script: capture fixture** → first unit `main()` in `scripts/capture_exchangeinfo_fixture.py`. (evidence: `scripts/capture_exchangeinfo_fixture.py` L9-L27)
- **Utility script: debug stage7 metrics** → first unit `main()` in `scripts/debug_stage7_metrics.py`. (evidence: `scripts/debug_stage7_metrics.py` L8-L37)
- **Utility script: debug stage7 schema** → first unit `main()` in `scripts/debug_stage7_schema.py`. (evidence: `scripts/debug_stage7_schema.py` L12-L32)
- **Quality guard script** → first unit `main()` in `scripts/guard_multiline.py`. (evidence: `scripts/guard_multiline.py` L83-L113)

### Schedulers/workers/crons

- **NOT FOUND** for Celery/APScheduler/RQ/cron workers in `src/`, `scripts/`, `tests/`.
  - Searched: `rg -n "cron|celery|apscheduler|rq|worker|beat|schedule\(|while True" src scripts tests`.
  - Found only internal loop runners (`cli.run_with_optional_loop`, backtest loop, sweep internal loop). (evidence: `src/btcbot/cli.py` L354-L451; `src/btcbot/services/stage7_single_cycle_driver.py` L58-L77; `src/btcbot/services/sweep_service.py` L76)

### Test harness entrypoints that execute full cycles

- `tests/test_cli.py` directly invokes `cli.run_cycle(...)` and `cli.run_cycle_stage4(...)` for full stage3/stage4 flow wiring checks. (evidence: `tests/test_cli.py` L67-L68, L73-L74, L710-L711)
- `tests/test_stage7_run_integration.py` invokes `cli.run_cycle_stage7(...)` and verifies persisted stage7 tables. (evidence: `tests/test_stage7_run_integration.py` L108-L116)
- `tests/test_stage4_cycle_runner.py` invokes `Stage4CycleRunner().run_one_cycle(...)` and validates audit output. (evidence: `tests/test_stage4_cycle_runner.py` L78-L97)

---

## 2) Entrypoint call-graphs (critical loops, with data + side effects)

## A. `btcbot` / `python -m btcbot` / `python -m btcbot.cli`

- `btcbot.cli.main()`
  - parses subcommands and args (`run`, `stage4-run`, `stage7-run`, `stage7-backtest`, etc.)
  - loads `Settings()` (env + `.env`) and configures logging
  - dispatches command handler
  - **Data models:** `Settings`, command arg namespace
  - **Side effects:** reads env/.env, stdout, logging. (evidence: `src/btcbot/cli.py` L63-L75, L246-L351; `src/btcbot/config.py` L15-L20)

### A1. Stage3 command `run`

- `main` -> `run_with_optional_loop(command="run", cycle_fn=run_cycle(...))`
  - optional retry loop with per-cycle backoff (1s,2s,4s capped by code path)
  - `run_cycle(settings, force_dry_run)`
    - live-side-effect policy check (`validate_live_side_effects_policy`)
    - build exchange adapter (`build_exchange_stage3`)
    - construct services (`PortfolioService`, `MarketDataService`, `AccountingService`, `StrategyService`, `RiskService`, `ExecutionService`, `SweepService`)
    - execute cycle:
      - `ExecutionService.cancel_stale_orders(cycle_id)`
      - `PortfolioService.get_balances()`
      - `MarketDataService.get_best_bids(symbols)`
      - `AccountingService.refresh(symbols, mark_prices)`
      - `StrategyService.generate(cycle_id, symbols, balances)` -> `Intent[]`
      - `RiskService.filter(cycle_id, intents)` -> approved `Intent[]`
      - `ExecutionService.execute_intents(approved_intents, cycle_id)`
      - `StateStore.set_last_cycle_id(cycle_id)`
  - **Data models:** `Balance`, `Intent`, `OrderIntent`, `Order`, `Position`, symbol maps
  - **Side effects:**
    - HTTP (BTCTurk public/private) via exchange adapter
    - DB writes (`actions`, `orders`, `fills`, `positions`, `meta`)
    - logs/stdout
  - (evidence: `src/btcbot/cli.py` L250-L258, L354-L451, L476-L605; `src/btcbot/services/exchange_factory.py` L17-L68; `src/btcbot/services/execution_service.py` L129-L245, L247-L356; `src/btcbot/accounting/accounting_service.py` L21-L41)

### A2. Stage4 command `stage4-run`

- `main` -> `run_with_optional_loop(command="stage4-run", cycle_fn=run_cycle_stage4(...))`
  - `run_cycle_stage4` does policy check and can persist `cycle_audit` block record when policy blocks
  - `Stage4CycleRunner.run_one_cycle(settings)`:
    - wires stage4 services (`ExchangeRulesService`, `AccountingService` stage4, `OrderLifecycleService`, `RiskPolicy`, `RiskBudgetService`, `ExecutionService` stage4, `DecisionPipelineService`, `AnomalyDetectorService`, `LedgerService`)
    - fetches fills per symbol (`AccountingService.fetch_new_fills`)
    - **transaction boundary #1:** `with state_store.transaction():` ingest ledger events + apply fills + update cursors
    - run decision pipeline + lifecycle planning + risk filtering + risk-budget mode + degrade decisions
    - execute actions (`execution_service.execute_with_report`)
    - persist anomalies/degrade and metrics
    - **transaction boundary #2:** persist cycle metrics in transaction
    - write `cycle_audit` and last cycle id
  - **Data models:** `Fill`, `Position`, `LifecycleAction`, `RiskDecision`, `PnLSnapshot`, `CycleMetrics`
  - **Side effects:**
    - HTTP exchange calls for market/open orders/fills/submit/cancel
    - DB writes to stage4/risk/anomaly/metrics/audit/ledger tables
  - (evidence: `src/btcbot/cli.py` L260-L268, L607-L654, L622-L637; `src/btcbot/services/stage4_cycle_runner.py` L62-L120, L170-L217, L250-L333, L396-L448, L416-L417, L545-L547, L632-L636)

### A3. Stage7 command `stage7-run`

- `main` -> `run_cycle_stage7(settings, include_adaptation)`
  - enforces `--dry-run` + `STAGE7_ENABLED`
  - `Stage7CycleRunner.run_one_cycle(...)`
    - (if not injected) executes `Stage4CycleRunner.run_one_cycle` first
    - calls `run_one_cycle_with_dependencies(...)`
      - creates services (`UniverseSelectionService`, `PortfolioPolicyService`, `OrderBuilderService`, `Stage7RiskBudgetService`, `OMSService`, `LedgerService`)
      - computes risk inputs -> `Stage7RiskDecision`
      - selects universe, resolves rules, computes final mode
      - builds order intents, runs OMS (`reconcile_open_orders`, `process_intents`)
      - materializes fills/events, appends ledger events
      - computes snapshot + run metrics
      - **transaction boundary:** `StateStore.save_stage7_cycle(...)` (internally transactional)
      - writes run metrics and optional adaptation
  - **Data models:** `Stage7RiskInputs`, `RiskDecision`, `OrderIntent`, `Stage7Order`, `OrderEvent`, `LedgerEvent`
  - **Side effects:**
    - HTTP/replay market reads
    - DB writes to `stage7_*`, `ledger_events`, `fills`, `positions`
  - (evidence: `src/btcbot/cli.py` L270-L275, L657-L682; `src/btcbot/services/stage7_cycle_runner.py` L43-L87, L89-L130, L246-L260, L262-L321, L420-L441, L555-L566, L678-L749, L764-L787)

### A4. Stage7 backtest command `stage7-backtest`

- `main` -> `run_stage7_backtest(...)`
  - validates dataset contract, builds `MarketDataReplay.from_folder`
  - `Stage7BacktestRunner.run(...)` -> `Stage7SingleCycleDriver.run(...)`
  - driver loops time steps and invokes `Stage7CycleRunner.run_one_cycle(...)` per step
  - **Data models:** replay candles/orderbook/ticker -> stage7 domain models
  - **Side effects:** file reads (dataset), SQLite writes (output DB), stdout JSON summary
  - (evidence: `src/btcbot/cli.py` L824-L880; `src/btcbot/services/market_data_replay.py` L110-L132, L159-L217; `src/btcbot/services/stage7_backtest_runner.py` L32-L71; `src/btcbot/services/stage7_single_cycle_driver.py` L27-L77)

### A5. Replay capture command `replay-capture`

- `main` -> `run_replay_capture(...)`
  - `capture_replay_dataset(ReplayCaptureConfig(...))`
    - init folders
    - loop polling BTCTurk public endpoints (ticker/orderbook)
    - atomic write CSV files
    - validate dataset at end
  - **Data models:** replay capture config + CSV row dicts
  - **Side effects:** HTTP GETs, file system writes (`data/replay/*`)
  - (evidence: `src/btcbot/cli.py` L1032-L1049; `src/btcbot/replay/tools.py` L109-L186)

---

## 3) Verified sequence flows (A-F)

## A) Bootstrap / startup

- **Trigger:** manual CLI invocation (`btcbot ...`). (evidence: `src/btcbot/cli.py` L63-L75)
- **Flow:** parse args -> `Settings()` (loads env and `.env`) -> `setup_logging` -> command dispatch. (evidence: `src/btcbot/cli.py` L246-L351; `src/btcbot/config.py` L15-L20)
- **Idempotency/dedupe:** none at startup; starts applying once cycle methods call state store methods.
- **Retry/backoff:** loop wrapper retries cycle exceptions up to 3 attempts with exponential sleep. (evidence: `src/btcbot/cli.py` L387-L418)
- **Transactions:** none at startup itself.
- **Failure modes:** invalid settings raise validation errors before cycle; loop returns non-zero when cycle fails repeatedly. (evidence: `src/btcbot/config.py` L487-L508; `src/btcbot/cli.py` L398-L411)

## B) Market data ingest (external -> persistence)

- **Trigger:** per cycle (manual single-shot or timed loop). (evidence: `src/btcbot/cli.py` L370-L371, L387-L393)
- **Flow (live/public mode):**
  - Exchange reads via `BtcturkHttpClient._get` with retry behavior.
  - Stage3 gets best bids (`MarketDataService.get_best_bids`) and feeds accounting/strategy.
  - Stage4 fetches new fills per symbol (`fetch_new_fills`) and persists within transaction (`ingest_exchange_updates`, `apply_fills`, cursor updates).
  - Stage7 replay reads dataset files (`MarketDataReplay.from_folder`) or exchange client methods.
  - (evidence: `src/btcbot/adapters/btcturk_http.py` L197-L232; `src/btcbot/services/market_data_service.py` L20-L35; `src/btcbot/services/accounting_service_stage4.py` L37-L87; `src/btcbot/services/stage4_cycle_runner.py` L195-L205; `src/btcbot/services/market_data_replay.py` L110-L132)
- **Idempotency/dedupe:** ledger/fill/event inserts use `INSERT OR IGNORE`; fill cursor avoids re-reading too far except lookback overlap. (evidence: `src/btcbot/services/state_store.py` L2411-L2441; `src/btcbot/services/accounting_service_stage4.py` L38-L46, L98-L101)
- **Retry/backoff:** HTTP retries for timeout/429/5xx with capped total wait. (evidence: `src/btcbot/adapters/btcturk_http.py` L49-L53, L103-L110, L201-L217)
- **Transactions:** stage4 ingest is wrapped in explicit transaction. (evidence: `src/btcbot/services/stage4_cycle_runner.py` L196-L205; `src/btcbot/services/state_store.py` L111-L127)
- **Failure modes:** per-symbol fill fetch errors are logged and symbol marked failed; transaction failures raise and fail cycle. (evidence: `src/btcbot/services/stage4_cycle_runner.py` L187-L193, L206-L217)

## C) Strategy decision (signal -> intents)

- **Trigger:** after balances + market/accounting context in cycle. (evidence: `src/btcbot/cli.py` L542-L553)
- **Flow:**
  - `StrategyService.generate` builds `StrategyContext` from orderbooks, positions, balances, open order counts.
  - `ProfitAwareStrategyV1.generate_intents` applies take-profit / conservative-entry rules.
  - Stage4 alternative: `DecisionPipelineService.run_cycle` produces order requests/allocation decisions.
  - Stage7 alternative: `PortfolioPolicyService` + `OrderBuilderService` produce stage7 order intents.
  - (evidence: `src/btcbot/services/strategy_service.py` L31-L67; `src/btcbot/strategies/profit_v1.py` L11-L67; `src/btcbot/services/stage4_cycle_runner.py` L250-L280; `src/btcbot/services/stage7_cycle_runner.py` L125-L129, L262-L273)
- **Idempotency/dedupe:** stage3 `Intent.create` carries idempotency key (ASSUMPTION: generated in intent model; verify in `src/btcbot/domain/intent.py`).
- **Retry/backoff:** none in strategy generation itself.
- **Transactions:** none around generation itself.
- **Failure modes:** missing market data/rules can skip symbols and continue (stage7 rules unavailable statuses). (evidence: `src/btcbot/services/stage7_cycle_runner.py` L280-L309)

## D) Risk gating (limits, drawdown caps, throttles, circuit-like modes)

- **Trigger:** immediately after intents/actions are produced.
- **Flow:**
  - Stage3: `RiskService.filter` -> `risk.policy.RiskPolicy.evaluate` (max orders, open-orders-per-symbol, cooldown, notional cap, min_notional quantization checks).
  - Stage4: `services.risk_policy.RiskPolicy.filter_actions` (daily loss, drawdown, max open orders, max position notional, min profit threshold).
  - Stage7: `Stage7RiskBudgetService.decide` computes `RiskMode` (`NORMAL/REDUCE_RISK_ONLY/OBSERVE_ONLY`) from drawdown/daily loss/stale data/spread/liquidity + cooldown monotonicity.
  - Stage7 OMS throttle: `TokenBucketRateLimiter.consume` emits THROTTLED path.
  - (evidence: `src/btcbot/services/risk_service.py` L15-L45; `src/btcbot/risk/policy.py` L42-L79, L81-L104; `src/btcbot/services/risk_policy.py` L40-L106; `src/btcbot/services/stage7_risk_budget_service.py` L29-L114; `src/btcbot/services/oms_service.py` L127-L131, L200-L215; `src/btcbot/services/rate_limiter.py` L36-L46)
- **Idempotency/dedupe:** stage7 mode + risk decision persisted each cycle; later writes are upserted by cycle id. (evidence: `src/btcbot/services/state_store.py` L896-L920, L923-L942)
- **Retry/backoff:** stage7 risk decision computation has no retry; OMS retries happen downstream.
- **Transactions:** risk decisions persisted in `save_stage7_cycle` transaction when included. (evidence: `src/btcbot/services/state_store.py` L807-L920)
- **Failure modes:** policy blocks return code 2 (stage3/stage4 live not armed/kill-switch); stage4 can still write policy-block audit envelope. (evidence: `src/btcbot/cli.py` L484-L491, L618-L637)

## E) Execution / OMS (placement -> retries -> reconciliation)

- **Trigger:** approved intents/actions available.
- **Flow (stage3/stage4):**
  - `ExecutionService.cancel_stale_orders`: stale detection -> dedupe action -> cancel or reconcile uncertain result.
  - `ExecutionService.execute_intents`: lifecycle refresh -> dedupe action -> dry-run metadata or live place + uncertain submit reconcile.
  - Live side effects require policy pass (`_ensure_live_side_effects_allowed`).
  - (evidence: `src/btcbot/services/execution_service.py` L129-L245, L247-L356, L628-L637)
- **Flow (stage7 OMS):**
  - `reconcile_open_orders` builds synthetic intents for non-terminal orders.
  - `process_intents`: throttle check -> idempotency registration -> retry_with_backoff on transient errors -> state transitions/events -> transactionally upsert orders + append events.
  - (evidence: `src/btcbot/services/oms_service.py` L381-L414, L112-L131, L200-L275, L376-L379)
- **Idempotency/dedupe:**
  - stage3 `record_action` dedupe key bucketed by action+payload+time window.
  - stage7 `try_register_idempotency_key` conflicts on payload mismatch; duplicates become `DUPLICATE_IGNORED`.
  - stage7 event append uses deterministic event IDs + insert-ignore semantics.
  - (evidence: `src/btcbot/services/state_store.py` L1511-L1535, L1201-L1224, L1160-L1178; `src/btcbot/services/oms_service.py` L217-L252)
- **Retry/backoff:**
  - HTTP adapter retries network/429/5xx.
  - OMS retry utility uses deterministic exponential backoff + jitter seed.
  - (evidence: `src/btcbot/adapters/btcturk_http.py` L201-L217; `src/btcbot/services/retry.py` L19-L58)
- **Transactions:**
  - stage7 OMS persists orders/events together in one transaction.
  - stage3 action writes are autocommit per `_connect` context.
  - (evidence: `src/btcbot/services/oms_service.py` L376-L379; `src/btcbot/services/state_store.py` L89-L103)
- **Failure modes:**
  - non-retryable placement/cancel logs and continue.
  - uncertain errors attempt reconciliation.
  - retry exhaustion emits `RETRY_GIVEUP` and continues with next intent.
  - (evidence: `src/btcbot/services/execution_service.py` L193-L228, L336-L355; `src/btcbot/services/oms_service.py` L275-L298)

## F) Ledger/accounting & metrics (events -> metrics -> reporting)

- **Trigger:** after fills are fetched/simulated in cycle.
- **Flow:**
  - stage4: ingest fills -> `LedgerService.ingest_exchange_updates` -> `AccountingService.apply_fills` -> snapshot/risk/metrics -> persist cycle metrics.
  - stage7: materialize fills from OMS FILLED orders -> append ledger events -> recompute ledger state/positions -> snapshot -> `save_stage7_cycle` + `save_stage7_run_metrics`.
  - report commands read run metrics and export JSONL/CSV.
  - (evidence: `src/btcbot/services/stage4_cycle_runner.py` L196-L200, L529-L547; `src/btcbot/services/ledger_service.py` L69-L114, L196-L257; `src/btcbot/services/stage7_cycle_runner.py` L469-L606, L678-L764; `src/btcbot/cli.py` L751-L760, L765-L960)
- **Idempotency/dedupe:** ledger events use `INSERT OR IGNORE` by `event_id`; fill apply guard `mark_fill_applied`. (evidence: `src/btcbot/services/state_store.py` L2283-L2309, L2411-L2441)
- **Retry/backoff:** no explicit retry around metric writes; relies on exception handling and transaction rollback.
- **Transactions:**
  - stage4 metrics persisted in explicit transaction.
  - stage7 cycle trace + intents + risk + run metrics + ledger metrics persisted in one transactional call.
  - (evidence: `src/btcbot/services/stage4_cycle_runner.py` L545-L547; `src/btcbot/services/state_store.py` L783-L972)
- **Failure modes:**
  - stage4 metrics persistence failure logs warning and continues.
  - stage7 save raises runtime error with stage marker (`cycle_trace_upsert`, `order_intents_upsert`, etc.) and bubbles to caller.
  - (evidence: `src/btcbot/services/stage4_cycle_runner.py` L548-L558; `src/btcbot/services/state_store.py` L876-L972)

---

## 4) Scenario checklist (triggers, dedupe, retries, tx boundaries, failure behavior)

### A) Bootstrap/startup
- Trigger: manual command execution. (evidence: `src/btcbot/cli.py` L63-L75)
- Dedupe: none.
- Retry: loop retries cycle execution only, not parser/settings creation. (evidence: `src/btcbot/cli.py` L387-L418)
- Tx boundary: none.
- Failure: settings validation or command handler returns non-zero. (evidence: `src/btcbot/config.py` L487-L508)

### B) Market ingest
- Trigger: cycle start / timer loop. (evidence: `src/btcbot/cli.py` L370-L393)
- Dedupe: cursor + insert-ignore event ids/fill ids. (evidence: `src/btcbot/services/accounting_service_stage4.py` L38-L46; `src/btcbot/services/state_store.py` L2411-L2441)
- Retry: HTTP retry in adapter. (evidence: `src/btcbot/adapters/btcturk_http.py` L201-L217)
- Tx boundary: stage4 fill+ledger+cursors transaction. (evidence: `src/btcbot/services/stage4_cycle_runner.py` L196-L205)
- Failure: per-symbol failures degrade and continue; transaction failure aborts cycle. (evidence: `src/btcbot/services/stage4_cycle_runner.py` L187-L217)

### C) Strategy decision
- Trigger: after balances/market/accounting refresh. (evidence: `src/btcbot/cli.py` L542-L553)
- Dedupe: risk service records approved intents (ASSUMPTION: dedupe semantics depend on intent idempotency key; verify `StateStore.record_intent` usage in `state_store.py`).
- Retry: none.
- Tx boundary: none explicit.
- Failure: invalid symbols/constraints lead to skips, not process crash in normal paths. (evidence: `src/btcbot/risk/policy.py` L63-L93)

### D) Risk gating
- Trigger: immediately post-intent generation / pre-execution.
- Dedupe: decisions persisted by cycle id upserts (stage7).
- Retry: none in risk compute itself.
- Tx boundary: included inside `save_stage7_cycle` transaction for stage7 risk decision.
- Failure: policy blocks side effects and can return code 2. (evidence: `src/btcbot/cli.py` L484-L491, L618-L637)

### E) Execution/OMS
- Trigger: approved intents/actions exist.
- Dedupe: stage3 `record_action`; stage7 idempotency key table + duplicate/conflict events.
- Retry: OMS transient retries + HTTP retries.
- Tx boundary: stage7 orders+events persisted together.
- Failure: uncertain errors reconciled; retry give-up continues.

### F) Ledger/accounting/metrics
- Trigger: after fills/events.
- Dedupe: `append_ledger_events` insert-ignore + `mark_fill_applied`.
- Retry: none explicit for DB persist.
- Tx boundary: stage4 metrics transaction; stage7 cycle transaction.
- Failure: warnings on non-critical metric write failures, hard errors on stage7 atomic save failures.

(Scenario D/E/F evidence: `src/btcbot/services/state_store.py` L1511-L1535, L1201-L1224, L2411-L2441, L783-L972; `src/btcbot/services/oms_service.py` L217-L252, L264-L298, L376-L379; `src/btcbot/services/stage4_cycle_runner.py` L545-L558)

---

## Single Page Map

**EntryPoints**
- `btcbot` / `python -m btcbot` / `python -m btcbot.cli` -> `cli.main`
- `stage7-backtest` path -> `run_stage7_backtest` -> `Stage7BacktestRunner.run` -> `Stage7SingleCycleDriver.run`
- `replay-capture` path -> `run_replay_capture` -> `capture_replay_dataset`

**Loops**
- `run_with_optional_loop` (stage3/stage4 timed loop with retry)
- `Stage7SingleCycleDriver.run` replay-step loop

**Core Services**
- Stage3: `MarketDataService`, `AccountingService`, `StrategyService`, `RiskService`, `ExecutionService`
- Stage4: `Stage4CycleRunner` + `DecisionPipelineService`, `RiskBudgetService`, `AnomalyDetectorService`, `ExecutionService` stage4
- Stage7: `Stage7CycleRunner` + `Stage7RiskBudgetService`, `UniverseSelectionService`, `PortfolioPolicyService`, `OrderBuilderService`, `OMSService`, `LedgerService`

**Persistence**
- `StateStore` (SQLite, WAL, busy timeout)
- Transactional blocks: `state_store.transaction()` and `save_stage7_cycle(...)`
- Key tables: `actions`, `orders`, `fills`, `ledger_events`, `cycle_audit`, `stage7_cycle_trace`, `stage7_run_metrics`, `stage7_order_events`, `stage7_idempotency_keys`

**External Integrations**
- BTCTurk HTTP (public + private endpoints via `BtcturkHttpClient`)
- Replay file datasets (`candles/`, `orderbook/`, `ticker/`) for backtest/replay modes

(evidence: `src/btcbot/cli.py` L354-L451, L824-L880, L1032-L1049; `src/btcbot/services/state_store.py` L89-L127, L783-L972; `src/btcbot/adapters/btcturk_http.py` L165-L232; `src/btcbot/services/market_data_replay.py` L110-L132)
