# BTCTurk Runtime Control-Flow Audit (Repository-Based)

## Scope
- Default runtime analyzed: `btcbot cli run` (Stage3 path).
- Also mapped: Stage4/Stage7 entrypoints where they differ materially.
- All statements below are grounded in repository code only.

## Process Model / Concurrency

### Observed model
- **Single OS process per DB/account scope** enforced by `single_instance_lock(db_path, account_key)` using file locks (`flock`/`msvcrt`).
- **Single-threaded synchronous cycle execution** in Stage3 runtime loop (`time.sleep`, direct function calls, sync HTTP client).
- **No internal worker thread pool / scheduler framework** in Stage3 loop.
- **Async primitives exist but are not in Stage3 runtime path**:
  - `BtcturkWsClient` uses `asyncio` tasks + an `asyncio.Queue` for WS read/dispatch.
  - `BtcturkRestClient` is async HTTP.

### Queue / scheduler inventory
| Component | Type | In active Stage3 path? | Notes |
|---|---|---:|---|
| `run_with_optional_loop` | timer loop | Yes | cycle timer + jitter + retry wrapper |
| `single_instance_lock` | process lock | Yes | prevents duplicate runtime per db/account |
| `BtcturkWsClient.queue` | `asyncio.Queue` | No (Stage3 path) | only in async WS adapter |

## Main Loop Trigger Model (what causes decisions)

### Stage3 (`btcbot cli run`)
- Trigger is **timer-driven** loop when `--loop` is set; otherwise one-shot cycle.
- Each cycle executes deterministic pull-based steps:
  1. Pull balances (`PortfolioService`).
  2. Pull market data snapshot (`MarketDataService`):
     - REST mode: direct REST orderbook fetch.
     - WS mode: reads in-memory WS cache; if stale and fallback enabled, uses REST fallback.
  3. If market-data freshness is stale => **fail-closed** cycle block (`market_data:stale`).
  4. Startup recovery / lifecycle refresh / accounting refresh.
  5. Strategy generate intents.
  6. Risk filter intents.
  7. Execution submit/cancel path.

### Stage4 / Stage7
- Stage4: timer loop wrapper can run repeatedly, but each iteration calls `Stage4CycleRunner.run_one_cycle`.
- Stage7: dry-run only cycle via `Stage7CycleRunner` (single-cycle command path).

## Sequence Diagram (text, numbered)

### Stage3 end-to-end decision cycle
1. Operator runs `python -m btcbot.cli run [--loop ...]`.
2. CLI loads/validates `Settings` and acquires `single_instance_lock(account_key="stage3")`.
3. Loop runner invokes `run_cycle()` (once or repeatedly on timer+jitter).
4. `run_cycle()` computes live policy gates (`DRY_RUN/KILL_SWITCH/LIVE_TRADING/ACK/SAFE_MODE`).
5. Build exchange adapter (`build_exchange_stage3` => `BtcturkHttpClient` or dry-run wrapper).
6. Create `StateStore` (SQLite, WAL, schema ensure).
7. Construct services: Portfolio, MarketData, Sweep, Execution, Accounting, Strategy, Risk.
8. Fetch balances + market bids/freshness.
9. If freshness stale => emit audit decision + return cycle success-with-block (no trade side effects).
10. Run startup recovery:
    - refresh lifecycle reconciliation,
    - accounting refresh if mark prices available,
    - invariant checks; may force observe-only.
11. Cancel stale orders (TTL path) with idempotency guards.
12. Refresh accounting/fills and compute cash/investable budget.
13. Strategy generates intents.
14. Risk policy normalizes/caps/blocks intents.
15. Execution path:
    - refresh lifecycle + prune idempotency keys,
    - enforce safe_mode/kill_switch/live-arm gates,
    - reserve idempotency key (`place_order`),
    - record deduped action,
    - submit exchange order (or dry-run simulate),
    - reconcile uncertain outcomes (`openOrders`/`allOrders`),
    - persist order + action metadata + idempotency final state.
16. Persist cycle metrics/audit outputs and return.
17. Loop sleeps `cycle_seconds + jitter` and repeats.

## State Machine (current + proposed explicit model)

### Current implicit runtime states (derived)
- `BOOTSTRAP`: parse args, load settings, secret checks.
- `LOCK_ACQUIRED`: singleton lock acquired.
- `CYCLE_START`: run_id/cycle_id + policy evaluation.
- `OBSERVE_ONLY`: safe mode or kill switch (planning allowed, writes blocked).
- `MARKET_DATA_BLOCKED`: stale/missing data fail-closed.
- `TRADING_EXECUTION`: intents filtered + execution path.
- `CYCLE_ERROR`: exception path (`ConfigurationError` => rc=2, other => rc=1).
- `SHUTDOWN`: flush instrumentation/logs, close exchange.

### Proposed explicit finite-state machine (recommended)
| State | Entered when | Exit transition |
|---|---|---|
| INIT | process start | settings valid -> LOCKING; invalid -> ERROR_FATAL |
| LOCKING | acquiring singleton lock | success -> READY; fail -> PAUSED_LOCKED |
| READY | pre-cycle checks done | timer tick -> EVALUATING |
| EVALUATING | portfolio/market/accounting/strategy/risk | stale data -> PAUSED_DATA; intents approved -> EXECUTING; none -> READY |
| EXECUTING | submit/cancel attempts | success -> READY; uncertain/reconcile pending -> DEGRADED_RECOVERY; exception -> ERROR_RETRYABLE |
| DEGRADED_RECOVERY | unknown/pending order recovery | recovered -> READY; threshold breach -> PAUSED_SAFETY |
| PAUSED_DATA | fail-closed market data stale | data fresh -> READY |
| PAUSED_SAFETY | kill-switch/safe-mode/guardrail | operator unpause & gates valid -> READY |
| ERROR_RETRYABLE | transient failures | backoff elapsed -> READY |
| ERROR_FATAL | config/schema/lock hard failure | operator intervention -> INIT |

## Order Submission Semantics (exactly-once vs at-least-once)

### What exists now
- **Intent/action dedupe**: SQLite idempotency table (`PRIMARY KEY(action_type,key)`), action dedupe key unique index, and reservation/finalize flow.
- **Client order identity**: deterministic `client_order_id` generated from intent; stored with orders; unique index on `orders.client_order_id`.
- **Uncertain-submit reconciliation**: on ambiguous submit failures, system probes open/all orders by client ID and marks `COMMITTED`/`UNKNOWN`/`FAILED` accordingly.
- **Stale pending recovery**: pending idempotency reservations can be recovered on later cycles.

### Semantic classification
- **Process-level effective semantics**: *near exactly-once intent execution* within one SQLite state domain, via idempotency+dedupe.
- **Exchange-level semantic**: still **at-least-once transport attempts** (retries can resend request); correctness depends on client-order-id reconciliation and exchange behavior.
- Therefore: **not provable exactly-once globally**, but includes strong compensating controls.

## State Storage & Crash/Restart Recovery

### Storage
- Primary durable store: **SQLite** (`STATE_DB_PATH`, default `btcbot_state.db`), WAL mode.
- Persisted entities include actions, orders, fills, positions, intents, idempotency keys, Stage4/Stage7 tables.
- No Redis/Kafka/etc observed in Stage3 runtime path.

### Recovery behavior after restart/crash
1. Startup acquires lock and opens same SQLite DB.
2. `StartupRecoveryService` runs lifecycle refresh + optional accounting refresh + invariant checks.
3. `ExecutionService.refresh_order_lifecycle` reconciles open/unknown orders against exchange open/all order endpoints.
4. Idempotency table is consulted each cycle; stale `PENDING` records are either recovered to `COMMITTED` or downgraded to `FAILED` for safe retry.
5. Expired idempotency keys are pruned.

## Failure / Recovery Paths

| Failure | Current handling | Recovery path | Residual risk |
|---|---|---|---|
| Singleton contention | lock acquisition fails, process exits rc=2 | operator waits/stops other process | no HA failover model in-repo |
| Market data stale/missing | fail-closed cycle block | next cycle retries data pull | prolonged stale => indefinite pause |
| Exchange submit uncertain | reconcile by client_order_id via open/all orders | mark COMMITTED/UNKNOWN/FAILED, keep idempotency state | unknown may persist under repeated API failures |
| Pending idempotency stuck | stale pending recovery routine | exponential recovery attempts then FAILED | delayed execution under repeated lookup failures |
| Reconciliation endpoint failures | exceptions logged, loop continues | next cycle retries | drift can persist without escalation threshold |

## Gaps & Fixes (file path + concrete change)

| Gap | Evidence | Suggested change |
|---|---|---|
| WS mode ingest not wired in Stage3 runtime path | `MarketDataService` exposes `set_ws_connected`/`ingest_ws_best_bid`; Stage3 cycle constructs `MarketDataService` but no WS producer attachment in `run_cycle`. | **`src/btcbot/cli.py`**: add explicit WS runner integration (or hard-fail when `market_data_mode=ws` and no active WS source). |
| Runtime state machine is implicit only | no explicit enum/state object driving transitions | **`src/btcbot/cli.py` + `src/btcbot/domain/...`**: add `RuntimeState` enum + structured transition logging (`from_state`, `to_state`, `trigger`). |
| Reconciliation exceptions can indefinitely degrade silently | broad `except Exception` + continue in `refresh_order_lifecycle` | **`src/btcbot/services/execution_service.py`**: track consecutive refresh failures per symbol and escalate to kill-switch/observe-only after threshold. |
| Idempotency semantics are strong but not surfaced as explicit operator status | no cycle-level summary of idempotency pending/unknown counts | **`src/btcbot/services/state_store.py` + `src/btcbot/cli.py`**: expose counters for `PENDING/UNKNOWN/FAILED` idempotency rows in cycle audit logs. |
| Active Stage3 adapter path differs from async reliability adapters | Stage3 factory builds `BtcturkHttpClient`; async rest/ws adapters are separate | **`src/btcbot/services/exchange_factory.py`**: either (A) wire async reliability adapters behind interface, or (B) remove/rename unused knobs to avoid operator confusion. |

## UNKNOWN (artifact needed)
- UNKNOWN: Production supervisor/orchestrator model (systemd/k8s/PM2/etc) and replica count.
  - Needed artifact: deployment manifests/runtime unit files.
- UNKNOWN: Exchange-side guarantees for duplicate `clientOrderId` handling across network retries.
  - Needed artifact: BTCTurk API contract version used in production.
