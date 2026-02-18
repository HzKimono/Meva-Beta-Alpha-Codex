# Production Architecture: BTC Turk Spot Trading Agent (5-Asset Universe)

This document defines a production-grade, testable architecture focused on explicit boundaries, contracts, resiliency, and observability.

## 1) Package tree (folders/files)

```text
src/btcbot/
  app/
    live_runner.py              # Continuous trading loop orchestration
    dependency_container.py     # Wire concrete adapters to interfaces

  contracts/
    exchange.py                 # ExchangeClient protocol
    strategy.py                 # Strategy protocol
    risk.py                     # RiskManager protocol
    portfolio.py                # Portfolio protocol
    order_manager.py            # OrderManager protocol
    data_feed.py                # DataFeed protocol

  domain/
    models.py                   # Immutable domain entities (Order, Fill, Position, Balance, Bar, Tick)
    events.py                   # Domain events (OrderPlaced, OrderFilled, RiskRejected)
    value_objects.py            # Money, Symbol, Quantity, Price, ClientOrderId

  config/
    settings.py                 # Typed configuration via pydantic-settings
    secrets.py                  # Secret loading adapters (env, vault)

  adapters/
    btcturk/
      rest_client.py            # Raw BTC Turk REST adapter
      ws_client.py              # Raw BTC Turk websocket adapter
      exchange_client.py        # Contract implementation with retries/idempotency hooks
      data_feed.py              # Contract implementation for market data + snapshots

  services/
    strategy_engine.py          # Invokes strategy with market/account state
    risk_engine.py              # Pre-trade + post-trade risk checks
    portfolio_service.py        # Self-financing accounting + valuations
    order_manager_service.py    # Intent->order lifecycle and idempotent transitions
    execution_service.py        # Submit/cancel/amend orchestration + reconciliation
    recovery_service.py         # Startup state rebuild + crash recovery

  persistence/
    state_store.py              # Durable state for orders, positions, cash, last offsets
    outbox.py                   # Reliable event/outbox pattern for side effects

  observability/
    logging.py                  # Structured logging setup + correlation IDs
    metrics.py                  # Prometheus/OpenTelemetry metrics emitters
    tracing.py                  # OpenTelemetry tracing spans and context propagation

  tests/
    contracts/                  # Contract tests for each Protocol implementation
    integration/                # End-to-end paper/live-sim scenarios
    resilience/                 # Retry, circuit breaker, partial outage tests
```

## 2) Responsibilities per module

- **app/live_runner.py**: Owns the continuous loop (`ingest -> decide -> risk -> execute -> reconcile -> persist`), heartbeat scheduling, graceful shutdown, and backoff state.
- **contracts/**: Stable interfaces for all business-critical components to enable replacement/mocking in tests.
- **domain/**: Pure business objects and invariants; no network/IO.
- **config/**: Centralized typed config with environment overlays (`dev/staging/prod`) and strict validation.
- **adapters/btcturk/**: Exchange-specific protocol translation only; no strategy/risk logic.
- **services/**: Orchestrate use-cases and enforce trading policy.
- **persistence/**: Atomic writes for self-financing ledger + order state; crash-safe resume.
- **observability/**: Unified logging/metrics/tracing conventions with correlation keys (`cycle_id`, `client_order_id`, `symbol`).

## 3) Key interfaces (Protocols / ABCs)

```python
# src/btcbot/contracts/exchange.py
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str  # BUY | SELL
    type: str  # LIMIT | MARKET
    quantity: Decimal
    price: Decimal | None
    client_order_id: str


@dataclass(frozen=True)
class ExchangeOrder:
    exchange_order_id: str
    client_order_id: str
    symbol: str
    status: str
    filled_quantity: Decimal


class ExchangeClient(Protocol):
    def get_balances(self) -> dict[str, Decimal]: ...
    def get_positions(self) -> dict[str, Decimal]: ...
    def submit_order(self, order: OrderRequest) -> ExchangeOrder: ...
    def cancel_order(self, symbol: str, client_order_id: str) -> ExchangeOrder: ...
    def get_order(self, symbol: str, client_order_id: str) -> ExchangeOrder | None: ...
    def list_open_orders(self, symbols: Sequence[str]) -> list[ExchangeOrder]: ...
```

```python
# src/btcbot/contracts/strategy.py
from typing import Protocol, Sequence
from btcbot.domain.models import MarketSnapshot, PortfolioState, OrderIntent


class Strategy(Protocol):
    def decide(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        tradable_symbols: Sequence[str],
    ) -> list[OrderIntent]: ...
```

```python
# src/btcbot/contracts/risk.py
from typing import Protocol
from btcbot.domain.models import OrderIntent, RiskDecision, PortfolioState


class RiskManager(Protocol):
    def pre_trade_check(self, intent: OrderIntent, portfolio: PortfolioState) -> RiskDecision: ...
    def post_trade_check(self, portfolio: PortfolioState) -> RiskDecision: ...
```

```python
# src/btcbot/contracts/portfolio.py
from typing import Protocol
from btcbot.domain.models import Fill, PortfolioState, ValuationSnapshot


class Portfolio(Protocol):
    def snapshot(self) -> PortfolioState: ...
    def apply_fill(self, fill: Fill) -> PortfolioState: ...
    def valuation(self) -> ValuationSnapshot: ...
```

```python
# src/btcbot/contracts/order_manager.py
from typing import Protocol
from btcbot.domain.models import OrderIntent, ManagedOrder, OrderState


class OrderManager(Protocol):
    def plan(self, intent: OrderIntent) -> ManagedOrder: ...
    def mark_submitted(self, managed: ManagedOrder, exchange_order_id: str) -> OrderState: ...
    def mark_fill(self, client_order_id: str, fill_qty: float, fill_price: float) -> OrderState: ...
    def mark_cancelled(self, client_order_id: str) -> OrderState: ...
```

```python
# src/btcbot/contracts/data_feed.py
from typing import Protocol, Iterable
from btcbot.domain.models import Tick, MarketSnapshot


class DataFeed(Protocol):
    def warm_start_snapshot(self) -> MarketSnapshot: ...
    def stream_ticks(self) -> Iterable[Tick]: ...
    def is_fresh(self, max_age_seconds: float) -> bool: ...
```

## 4) Critical design constraints

### 4.1 Five-asset universe
- Configure exact symbol allowlist (length must equal 5 at startup validation).
- Exchange adapter and strategy engine only process allowed symbols.
- Risk engine rejects intents outside allowlist.

### 4.2 Continuous live loop
- Fixed cadence with jitter-aware scheduler.
- Each cycle has `cycle_id` and deterministic step ordering.
- If any critical dependency is degraded (stale data, exchange unreachable), transition to **safe mode** (cancel unprotected orders, no new entries).

### 4.3 Self-financing capital management
- Portfolio updates are fill-driven and double-entry (asset leg + quote leg + fee leg).
- No external cash injection during runtime; all sizing uses available free balance and realized PnL.
- Pre-trade check enforces `cash_after_trade >= reserved_buffer`.

### 4.4 Strict risk controls
- Per-asset max notional and max position.
- Portfolio max gross exposure and concentration limit.
- Daily loss limit and circuit breaker (halt new orders, keep protective exits).
- Market data freshness gate.

### 4.5 Idempotent order management
- Deterministic `client_order_id` composed from `(strategy_id, cycle_id, symbol, side, intent_hash)`.
- Before submit: check durable state + exchange open orders for existing `client_order_id`.
- Reconciliation loop repairs drift (`local accepted`, `exchange missing`, partial fills).
- Exactly-once effects via outbox + state transaction boundary.

### 4.6 Resiliency to API failures
- Retry policy with bounded exponential backoff + jitter for transient errors.
- Circuit breaker around each exchange endpoint group (orders, account, market data).
- Fallback from websocket to REST polling on stream interruptions.
- Recovery service rehydrates in-memory state from persistence and exchange truth on restart.

### 4.7 Full observability
- Structured logs (JSON), redacted secrets, correlation keys in every record.
- Metrics: cycle latency, decision count, risk rejects, order submit success/failure, fill slippage, PnL, drawdown, reconciliation drift.
- Tracing spans around external IO and each pipeline stage.
- Alerting hooks: stale feed, repeated retries, circuit-open, rejected-by-risk spikes.

## 5) Config & secrets management approach

- **Settings model** (`pydantic-settings`) as single source of truth.
- Layered config precedence: defaults < `.env` < environment variables < runtime overrides.
- `TradingUniverseConfig` validates exactly 5 symbols.
- `RiskConfig` includes hard limits (not strategy-tunable at runtime without restart).
- Secrets never committed; load from env or secret manager abstraction (`secrets.py`).
- Startup checks fail fast if required secrets/config are missing or malformed.

Example top-level settings groups:
- `exchange`: base_url, websocket_url, api_timeout, recv_window
- `auth`: api_key, api_secret (secret refs)
- `trading`: symbols[5], cycle_interval_ms, dry_run
- `risk`: max_notional_per_asset, max_position_per_asset, max_daily_loss
- `resilience`: retry_max_attempts, breaker_threshold, breaker_cooldown_s
- `observability`: log_level, metrics_port, otel_exporter_endpoint

## 6) Logging / metrics / tracing design

- **Logging**
  - JSON log format with stable fields: `timestamp`, `level`, `service`, `cycle_id`, `symbol`, `client_order_id`, `event`, `outcome`, `latency_ms`.
  - Redaction filter masks secrets and auth headers.
  - Use semantic events (`risk.reject`, `order.submit.success`, `reconcile.mismatch`).

- **Metrics**
  - Counters: `orders_submitted_total`, `orders_rejected_total`, `api_errors_total`, `circuit_open_total`.
  - Histograms: `cycle_duration_ms`, `api_latency_ms`, `slippage_bps`.
  - Gauges: `portfolio_equity`, `free_quote_balance`, `drawdown_pct`, `market_data_age_seconds`.

- **Tracing**
  - Root span per cycle: `live_cycle`.
  - Child spans: `data_feed.update`, `strategy.decide`, `risk.pre_trade`, `execution.submit`, `reconcile.sync`.
  - Propagate trace context through retries and asynchronous callbacks.

## 7) Testability strategy (contract-first)

- Contract tests for each `Protocol` implementation (shared fixtures).
- Deterministic replay tests for idempotency and self-financing invariants.
- Resilience tests with injected failures: timeout, 429, 5xx, stale data, duplicate fills.
- End-to-end integration tests in paper mode validating:
  1. no orders outside 5-symbol universe,
  2. risk limits block violations,
  3. reconciliation restores consistency after crash/restart.

## 8) Runtime model proposal: `asyncio` event loop (recommended)

### 8.1 Option analysis: asyncio vs threads vs sync loop

- **`asyncio` (recommended)**
  - Best fit for exchange-heavy I/O (REST + websocket + timers) with one process and explicit cooperative scheduling.
  - Enables precise rate-limit token sharing across tasks (data feed, reconcile, submit/cancel).
  - Keeps deterministic behavior by centralizing state mutation in one coordinator task (single writer pattern).
  - Failure handling is explicit per awaited boundary (timeouts, retry wrappers, circuit checks).

- **Threads**
  - Good for blocking SDKs, but increases race-condition surface for order/portfolio state unless strict locks or queue ownership are used.
  - Harder to guarantee determinism because interleavings are timing-dependent.
  - Viable fallback only if exchange client is fundamentally blocking and cannot be adapted.

- **Synchronous loop**
  - Highest determinism and easiest mental model.
  - But poor latency overlap: market data wait, REST calls, and persistence become serialized; can underutilize rate-limit windows and react slowly.
  - Acceptable for very low-frequency systems; less ideal for continuous live operation with websocket + reconciliation.

### 8.2 Why `asyncio` is the right default here

- Exchange I/O is predominantly network-bound and benefits from concurrent awaitable operations.
- BTC Turk rate-limits can be modeled as shared async limiters to coordinate all API consumers.
- Determinism is preserved by funneling order-state/portfolio-state mutations through one sequential actor (`CycleCoordinator`).

## 9) Concrete event-loop design (pseudocode only)

### 9.1 Top-level tasks and ownership

- `MarketDataTask`: websocket first, REST poll fallback, publishes snapshots/ticks.
- `CycleSchedulerTask`: emits cycle ticks at fixed cadence.
- `ExecutionTask`: submits/cancels orders and reconciles exchange status.
- `PersistenceTask`: commits checkpointed state and outbox atomically.
- `Supervisor`: lifecycle, health checks, graceful shutdown orchestration.

**Single-writer rule:** only `CycleCoordinator` mutates in-memory `PortfolioState` and `OrderState`; other tasks send messages/events.

### 9.2 Tick scheduling (websocket + polling placeholder)

```python
async def market_data_task(bus, ws_client, rest_client, cfg, shutdown):
    mode = "ws"
    retry = RetryPolicy(base=0.25, cap=8.0, jitter="full")

    while not shutdown.is_set():
        if mode == "ws":
            try:
                async for tick in ws_client.stream_ticks(symbols=cfg.symbols5):
                    await bus.publish(MarketTick(tick))
            except TransientError:
                await bus.publish(FeedDegraded(reason="ws_error"))
                await asyncio.sleep(retry.next_delay())
                mode = "poll" if retry.exhausted_soft_threshold() else "ws"
            else:
                retry.reset()
        else:
            try:
                snapshot = await rest_client.fetch_snapshot(symbols=cfg.symbols5)
                await bus.publish(MarketSnapshotEvent(snapshot))
                await asyncio.sleep(cfg.poll_interval_s)
            except TransientError:
                await asyncio.sleep(retry.next_delay())
            if ws_client.healthcheck_ok_for(cfg.ws_rejoin_after_s):
                mode = "ws"
                retry.reset()


async def cycle_scheduler_task(bus, cfg, shutdown):
    # deterministic cadence using monotonic clock
    next_t = monotonic()
    while not shutdown.is_set():
        next_t += cfg.cycle_interval_s
        await asyncio.sleep(max(0.0, next_t - monotonic()))
        await bus.publish(CycleTick(ts=utcnow()))
```

### 9.3 Backoff / retry strategy

```python
class RetryPolicy:
    # bounded exponential backoff with full jitter
    def next_delay(self):
        raw = min(cap, base * (2 ** attempts))
        attempts += 1
        return random.uniform(0, raw)


async def call_with_retry(op, retry_policy, timeout_s, breaker, idempotency_key=None):
    for _ in range(retry_policy.max_attempts):
        if breaker.is_open():
            raise CircuitOpen()
        try:
            with tracer.span("exchange_call"):
                return await asyncio.wait_for(op(idempotency_key=idempotency_key), timeout=timeout_s)
        except TransientError as e:
            breaker.record_failure(e)
            await asyncio.sleep(retry_policy.next_delay())
            continue
    raise RetryExhausted()
```

Policy notes:
- Retry only transient classes (`timeout`, `429`, `5xx`, connection reset).
- Never blindly retry non-idempotent submit without a deterministic `client_order_id`.
- Use endpoint-specific retry budgets to avoid starving reconciliation.

### 9.4 Circuit breaker behavior

```python
class CircuitBreaker:
    # states: CLOSED -> OPEN -> HALF_OPEN
    def on_result(self, ok):
        if state == CLOSED and consecutive_failures >= threshold:
            state = OPEN; opened_at = now()
        elif state == OPEN and now() - opened_at >= cooldown_s:
            state = HALF_OPEN; probe_budget = n_probes
        elif state == HALF_OPEN:
            if ok and probe_budget_depleted():
                state = CLOSED; reset_counters()
            elif not ok:
                state = OPEN; opened_at = now()


# Separate breaker instances:
breaker_orders = CircuitBreaker(...)
breaker_account = CircuitBreaker(...)
breaker_market_data = CircuitBreaker(...)
```

Behavioral contract:
- `orders` breaker OPEN ⇒ block new entries, allow protective cancels/exit reductions when possible.
- `market_data` breaker OPEN or stale feed ⇒ safe mode (no new risk-increasing intents).
- `account` breaker OPEN ⇒ freeze sizing updates; continue only with conservative protections.

### 9.5 Graceful shutdown

```python
async def shutdown_sequence(supervisor, tasks, bus, persistence, cfg):
    supervisor.set_shutdown_flag()
    await bus.publish(ShutdownRequested())

    # stop accepting new strategy intents
    await supervisor.pause_cycle_scheduler()

    # optional: cancel non-protective open orders
    if cfg.cancel_open_orders_on_shutdown:
        await supervisor.cancel_non_protective_orders(deadline_s=cfg.shutdown_deadline_s)

    # flush in-flight events and checkpoint state
    await supervisor.drain_event_bus(timeout_s=cfg.shutdown_drain_timeout_s)
    await persistence.checkpoint(reason="shutdown")

    # cancel remaining tasks and await completion
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
```

### 9.6 State persistence checkpoints

```python
async def cycle_coordinator(bus, stores, shutdown):
    cycle_no = 0
    while not shutdown.is_set():
        event = await bus.next_event()

        if isinstance(event, CycleTick):
            cycle_no += 1
            begin_tx()
            # 1) snapshot market/account views
            # 2) strategy.decide
            # 3) risk.pre_trade gate
            # 4) order intents -> execution commands
            # 5) apply fills/ledger deltas seen this cycle
            commit_tx()  # atomic: order state + portfolio ledger + outbox

            if cycle_no % 1 == 0:
                await stores.checkpoint(tag=f"cycle:{cycle_no}")

        elif isinstance(event, FillEvent):
            begin_tx(); apply_fill_and_ledger(event); commit_tx()

        elif isinstance(event, ReconcileEvent):
            begin_tx(); apply_reconciliation(event); commit_tx()
```

Checkpoint policy:
- **Hard checkpoint every cycle** (small state, stronger crash recovery).
- Additional checkpoint on critical transitions: breaker state change, risk halt, shutdown.
- Recovery boot sequence: load last checkpoint → replay outbox/ledger offsets → reconcile against exchange truth.

---

This architecture keeps strategy logic isolated, enforces risk/accounting invariants at service boundaries, and makes live operation observable and recoverable under real exchange failure modes.
