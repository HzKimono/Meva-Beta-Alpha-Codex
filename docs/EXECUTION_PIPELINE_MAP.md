# Trading/Execution Pipeline Deep Dive

## Execution call graph

### Stage 3 (`btcbot run`)

1. `btcbot.cli:run_cycle`
   - bootstraps `PortfolioService`, `MarketDataService`, `AccountingService`, `StrategyService`, `RiskService`, `ExecutionService`.
2. `PortfolioService.get_balances()` -> exchange private balances.
3. `MarketDataService.get_best_bids()` -> orderbook bids for active symbols.
4. `AccountingService.refresh()` -> ingest fills/update positions.
5. `StrategyService.generate()` -> `Intent` objects.
6. `RiskService.filter()` -> `RiskPolicy.evaluate()`; drops intents breaching open-order/cooldown/notional/cash-target constraints.
7. `ExecutionService.execute_intents()`:
   - lifecycle refresh via `refresh_order_lifecycle()`
   - block writes on `safe_mode`/`kill_switch` or arming policy failures
   - dedupe with `StateStore.record_action(...)` (`action_type + payload_hash + dedupe bucket`)
   - live path quantizes/validates then `exchange.place_limit_order(...)`
   - uncertain submit/cancel errors -> reconcile path (`_reconcile_submit` / `_reconcile_cancel`) before recording final status.
8. `ExecutionService.cancel_stale_orders()` runs each cycle before submission.

### Stage 4 (`btcbot stage4-run`)

1. `btcbot.cli:run_cycle_stage4` -> `Stage4CycleRunner.run_one_cycle` under `single_instance_lock`.
2. `OrderLifecycleService.plan()` builds lifecycle actions (`CANCEL`, `SUBMIT`) with cancel/replace semantics:
   - stale existing order -> `replace_cancel` + `replace_submit`.
3. `RiskPolicy.filter_actions()` gates lifecycle actions by open-order, position-notional, drawdown/daily-loss, and sell-side min-profit.
4. `ExecutionServiceStage4.execute_with_report()` performs writes:
   - dedupe via `StateStore.stage4_submit_dedupe_status(...)`
   - quantize/min-notional checks via `ExchangeRulesService`
   - dry-run -> simulated submit records
   - live -> `exchange.submit_limit_order(...)` and records stage4 order rows
   - cancel path uses `cancel_order_by_exchange_id(...)` and terminal checks.

### Stage 7 (`btcbot stage7-run` / backtest)

1. `Stage7CycleRunner` optionally executes Stage 4 first, then computes stage7 intents.
2. `OrderBuilderService.build_intents()` produces **LIMIT-only** `OrderIntent` (`order_type: Literal["LIMIT"]`).
3. `OMSService.process_intents()`:
   - registers idempotency key `submit:{client_order_id}` via `StateStore.try_register_idempotency_key`
   - retries transient adapter failures (`NetworkTimeout`, `RateLimitError`, `TemporaryUnavailable`) with exponential backoff
   - emits event stream (`SUBMIT_REQUESTED` -> `ACKED` -> `PARTIAL_FILL?` -> `FILLED` or `REJECTED`)
   - appends events + upserts orders transactionally.

## Order state model / state machine

### Stage 3 state model

- Persisted order statuses are in `domain.models.OrderStatus`:
  - `new`, `open`, `partial`, `filled`, `canceled`, `rejected`, `unknown`.
- Transitions happen in `services/execution_service.py` + reconcile calls:
  - submit success: `new` then updated to `open` when exchange/open-orders confirms.
  - cancel success: `open/partial -> canceled`.
  - uncertain failures: moved to `unknown` or reconciled to `filled/canceled` after `openOrders/allOrders/order` checks.
  - lifecycle refresh remaps `ExchangeOrderStatus` to local `OrderStatus`.

### Stage 4 state model

- `stage4_orders.status` values include: `submitted/open/cancel_requested/canceled/filled/rejected/error/unknown_closed`.
- Key transitions (modules):
  - `execution_service_stage4.py`: `open -> cancel_requested -> canceled`, submit -> `open`, failed submit -> `rejected/error`.
  - `reconcile_service` + `Stage4CycleRunner`: enrich/migrate to `unknown_closed` and import external orders.
  - `is_order_terminal()` treats `filled/canceled/rejected/unknown_closed` as terminal.

### Stage 7 state model (explicit FSM)

`domain.order_state.OrderStatus`:
- `PLANNED -> SUBMITTED -> ACKED -> PARTIALLY_FILLED -> FILLED`
- alternative terminal branches:
  - `PLANNED|SUBMITTED|ACKED -> REJECTED`
  - `PLANNED|SUBMITTED|ACKED|PARTIALLY_FILLED -> CANCELED`
- terminal states are immutable (`FILLED`, `CANCELED`, `REJECTED`).

Transition guard is implemented in `OMSService._transition_once` via `allowed_transitions` map.

## Idempotency strategy

### Stage 3

- Strategy intent has stable `idempotency_key` (`Intent.create` / `build_idempotency_key`).
- Execution dedupe is enforced at action-log layer:
  - `StateStore.record_action` computes dedupe key: `action_type:payload_hash:time_bucket`.
  - duplicate action within window returns `None` and is skipped.
- Client order id is deterministic from order content (`make_client_order_id`).
- For ambiguous submit/cancel outcomes, reconciliation probes exchange before deciding whether to persist/advance status.

### Stage 4

- Two client-id domains:
  - internal `client_order_id`
  - exchange-safe `exchange_client_id` (from `build_exchange_client_id`).
- Submit dedupe uses `stage4_submit_dedupe_status`:
  - blocks when matching order is `open`, `submitted/cancel_requested` (in-flight), or very recent `filled`.
- This prevents duplicate live submits across retries/restarts.

### Stage 7

- Hard idempotency table (`stage7_idempotency_keys`):
  - same key + same payload hash => duplicate ignored (`False`)
  - same key + different payload hash => `IdempotencyConflictError`.
- Event IDs are deterministic (`make_event_id(client_order_id, seq, event_type)`), and inserts are `INSERT OR IGNORE`.
- This gives replay/crash-recovery determinism.

## Balances/positions checks before order placement

### Stage 3

- Balances are read from exchange (`PortfolioService.get_balances`).
- TRY free cash is extracted in `cli.run_cycle` and converted into `investable_try = max(0, cash_try_free - try_cash_target)`.
- `RiskService/RiskPolicy` enforces investable cash, per-order/per-cycle notional caps, open-order limits, cooldown, and exchange min-notional/tick/step quantization.
- Live submits are re-validated against symbol rules just before submit in `ExecutionService` (`get_symbol_rules`, quantize, `validate_order`).

### Stage 4

- `AccountSnapshotService` builds holdings/cash/equity from private balances + mark prices and flags missing/bad data.
- Planning/risk pipeline uses snapshot + positions/open-orders before producing lifecycle actions.
- Execution re-validates per-symbol rules and min-notional at submission time.

### Stage 7

- Uses balances + mark prices to build exposure snapshot/policy plan; order builder emits skipped intents when rules/price/notional invalid.
- OMS also rejects invalid intents (`qty/price/notional <= 0`) before any progression.

## Error handling and resilience

### Retries / exponential backoff

- Shared sync helper: `services.retry.retry_with_backoff`:
  - exponential backoff with jitter (`base_delay * 2^(attempt-1)` bounded by max)
  - honors `Retry-After` when provided.
- `btcturk_http.py` uses this for REST GET/private GET call wrappers (timeouts/transport/HTTP errors).
- `AccountSnapshotService` retries balances/orderbook reads.
- Stage 7 OMS retries transient simulated adapter errors with configurable attempts/delays.
- New async adapter (`adapters/btcturk/rest_client.py`) has explicit retry classification and safe submit/cancel wrappers.

### Rate-limit handling

- HTTP 429 is classified retryable in both sync and async stacks.
- Async adapter has token-bucket limiter (`AsyncTokenBucket`) before each request.
- Stage 7 OMS can model rate-limit failures and retry accordingly.

### Circuit breaker

- No true open/half-open circuit breaker implementation is present.
- `circuit_breaker_state` metric currently mirrors kill-switch state, not a failure-rate breaker.

## Critical failure modes + fixes

1. **Duplicate live submits after timeout/5xx ambiguity (Stage 3 path)**
   - Current mitigation: uncertain-error reconciliation + action dedupe window.
   - Gap: stage3 submit path does not call the newer `submit_order_safe` reconciliation-by-client-id helper.
   - Fix: route live submit/cancel through `adapters/btcturk/rest_client.py` safe methods or add equivalent logic in `ExecutionService`.

2. **No hard circuit breaker under repeated exchange failures**
   - Current mitigation: retries + kill-switch/manual arming gates.
   - Fix: implement automatic breaker (failure-rate window, cool-off, half-open probes) and wire to execution gating.

3. **State divergence after process crash between exchange ack and DB write**
   - Current mitigation: startup recovery + reconcile/open/all orders polling; Stage7 deterministic idempotency table.
   - Fix: persist pending submit intent before network call and finalize with durable two-phase submit journal.

4. **Adapter split risk (legacy monolithic `btcturk_http.py` vs newer async reliability stack)**
   - Current mitigation: tests in both stacks.
   - Fix: unify production path onto one reliability stack; keep compatibility facade only.

5. **Market order support absent (LIMIT-only), possible non-fill in fast markets**
   - Current behavior: all intent/order models force limit orders.
   - Fix: add explicit market-order type with tighter risk caps and slippage guards, or maintain limit-only as policy and document execution quality constraints.

6. **Balance staleness / private endpoint degradation**
   - Current mitigation: retries + fallback TRY cash + flags (`missing_private_data`).
   - Fix: hard-stop live buying when private balances unavailable; allow sell-only degrade mode automatically.

## Notes on market vs limit / cancel-replace

- **Order type**: execution is LIMIT-only across stage3/stage4/stage7 models and builders.
- **Cancel/replace**: implemented in Stage 4 planning via `OrderLifecycleService.plan` (`replace_cancel` then `replace_submit`).
- **Stage 3** performs stale cancels and fresh submits, but no explicit atomic exchange-level replace endpoint usage.
