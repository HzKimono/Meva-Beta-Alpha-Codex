# Python Repo Architecture Spec (Source of Truth)

## 1) Repository tree (folders + key files)

```text
meva-agent/
├── pyproject.toml
├── README.md
├── Makefile
├── .env.example
├── config/
│   ├── base.yaml
│   ├── dev.yaml
│   ├── stage.yaml
│   ├── prod.yaml
│   ├── logging.yaml
│   └── schema.py
├── agent/
│   ├── __init__.py
│   ├── app.py
│   ├── orchestrator.py
│   ├── runtime_state.py
│   ├── event_bus.py
│   └── health.py
├── data/
│   ├── __init__.py
│   ├── models.py
│   ├── market_data_service.py
│   ├── ws_client.py
│   ├── rest_client.py
│   ├── normalizer.py
│   ├── feature_store.py
│   └── replay_loader.py
├── signals/
│   ├── __init__.py
│   ├── interfaces.py
│   ├── signal_engine.py
│   ├── strategy_registry.py
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── mean_reversion.py
│   │   └── momentum.py
│   └── validation.py
├── risk/
│   ├── __init__.py
│   ├── models.py
│   ├── limits_engine.py
│   ├── loss_guard.py
│   ├── exposure_engine.py
│   ├── kill_switch.py
│   └── policy_service.py
├── execution/
│   ├── __init__.py
│   ├── models.py
│   ├── order_service.py
│   ├── idempotency.py
│   ├── reconcile_service.py
│   ├── exchange_adapter.py
│   └── mock_exchange_adapter.py
├── portfolio/
│   ├── __init__.py
│   ├── models.py
│   ├── state_store.py
│   ├── valuation_service.py
│   ├── pnl_service.py
│   ├── treasury_service.py
│   └── ledger_service.py
├── ops/
│   ├── __init__.py
│   ├── logging.py
│   ├── metrics.py
│   ├── alerts.py
│   ├── reporting.py
│   └── controls_api.py
├── scripts/
│   ├── run_agent.py
│   ├── run_replay.py
│   ├── smoke_paper.sh
│   ├── backfill_market_data.py
│   └── rotate_cost_ledger.py
└── tests/
    ├── unit/
    │   ├── test_signal_engine.py
    │   ├── test_limits_engine.py
    │   ├── test_loss_guard.py
    │   ├── test_order_idempotency.py
    │   └── test_treasury_service.py
    ├── integration/
    │   ├── test_exchange_adapter_contract.py
    │   ├── test_orchestrator_pipeline.py
    │   ├── test_reconcile_restart_recovery.py
    │   └── test_kill_switch_flow.py
    ├── replay/
    │   ├── fixtures/
    │   ├── test_deterministic_replay.py
    │   └── test_strategy_parity.py
    └── sandbox/
        ├── test_paper_trading_e2e.py
        └── test_shadow_mode_e2e.py
```

---

## 2) Module boundaries, responsibilities, and public APIs

### `data/`
**Responsibilities**
- Acquire market data via WebSocket + REST backfill.
- Normalize exchange payloads into canonical market events.
- Validate freshness/sequence and persist snapshots/events.

**Public APIs (pseudocode)**
```python
class MarketDataService:
    def start_streams(self, symbols: list[str]) -> None
    def stop_streams(self) -> None
    def get_latest_snapshot(self, symbol: str) -> MarketSnapshot
    def get_feature_frame(self, symbol: str, window: int) -> FeatureFrame

class ReplayLoader:
    def load_events(self, source_path: str, start_ts: datetime, end_ts: datetime) -> Iterator[MarketEvent]
```

**Inputs / Outputs**
- Input: exchange ws/rest payloads, symbol list.
- Output: `MarketEvent`, `MarketSnapshot`, `FeatureFrame`.

### `signals/`
**Responsibilities**
- Build strategy context from features.
- Generate normalized trade intents with confidence/edge.
- Enforce signal validity/expiry rules.

**Public APIs (pseudocode)**
```python
class Strategy(Protocol):
    def generate_signal(self, ctx: SignalContext) -> Signal | None

class SignalEngine:
    def evaluate(self, symbol: str, ctx: SignalContext) -> SignalDecision

class StrategyRegistry:
    def get(self, strategy_name: str) -> Strategy
```

**Inputs / Outputs**
- Input: `SignalContext` (features, microstructure stats, runtime flags).
- Output: `Signal` or `None`, wrapped in `SignalDecision` with reject reason.

### `risk/`
**Responsibilities**
- Apply pre-trade and runtime risk policies.
- Enforce daily loss limits and drawdown caps.
- Manage kill-switch and circuit-breaker state.

**Public APIs (pseudocode)**
```python
class RiskPolicyService:
    def validate_intent(self, intent: TradeIntent, state: CanonicalState) -> RiskDecision

class LossGuard:
    def check_daily_loss(self, pnl_today: Decimal, limits: DailyLossLimit) -> LossCheckResult

class KillSwitchService:
    def activate(self, reason: str, actor: str) -> None
    def deactivate(self, actor: str) -> None
    def is_active(self) -> bool
```

**Inputs / Outputs**
- Input: `TradeIntent`, current `CanonicalState`, `RiskLimitsConfig`.
- Output: `RiskDecision` (approved/rejected, codes), kill-switch state transitions.

### `execution/`
**Responsibilities**
- Map approved intents to exchange-native orders.
- Guarantee idempotent submission and retry safety.
- Reconcile local order state with exchange truth.

**Public APIs (pseudocode)**
```python
class ExchangeAdapter(Protocol):
    def place_order(self, req: PlaceOrderRequest) -> ExchangeOrderAck
    def cancel_order(self, req: CancelOrderRequest) -> ExchangeCancelAck
    def fetch_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]
    def fetch_balances(self) -> list[ExchangeBalance]

class OrderService:
    def submit(self, intent: TradeIntent, idem_key: str) -> SubmitResult
    def cancel_all(self, reason: str) -> CancelAllResult

class ReconcileService:
    def reconcile(self, state: CanonicalState) -> ReconcileReport
```

**Inputs / Outputs**
- Input: approved intents, idempotency keys, adapter responses.
- Output: persisted `OrderRecord` transitions + reconciliation diffs.

### `portfolio/`
**Responsibilities**
- Maintain balances, positions, and valuations.
- Compute realized/unrealized PnL and fees.
- Track self-financing surplus and treasury allocations.

**Public APIs (pseudocode)**
```python
class PortfolioStateStore:
    def load(self) -> CanonicalState
    def save(self, state: CanonicalState) -> None

class PnLService:
    def update_from_fill(self, fill: FillEvent, state: CanonicalState) -> PnLUpdate

class TreasuryService:
    def compute_daily_surplus(self, state: CanonicalState) -> SurplusReport
    def evaluate_transfer(self, report: SurplusReport, policy: TreasuryPolicy) -> TransferDecision
```

**Inputs / Outputs**
- Input: fills, balances, marks, cost ledger entries.
- Output: updated state snapshots, PnL reports, treasury decisions.

### `ops/`
**Responsibilities**
- Structured logging, metrics emission, and alerting.
- Operator control surface (pause/resume, risk budget updates, kill-switch).
- Daily/weekly operational and financial reporting.

**Public APIs (pseudocode)**
```python
class MetricsSink:
    def incr(self, name: str, value: int = 1, tags: dict[str, str] | None = None) -> None
    def gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None
    def timing_ms(self, name: str, value: float, tags: dict[str, str] | None = None) -> None

class AlertService:
    def trigger(self, level: str, code: str, message: str, context: dict[str, Any]) -> None

class ControlsAPI:
    def pause_trading(self, actor: str, reason: str) -> ControlResult
    def resume_trading(self, actor: str) -> ControlResult
    def set_risk_budget(self, actor: str, new_budget: Decimal) -> ControlResult
```

**Inputs / Outputs**
- Input: internal events, policy breaches, operator commands.
- Output: log events, metric series, alerts, persisted control actions.

### `agent/`
**Responsibilities**
- Orchestrate deterministic pipeline as source-of-truth runtime.
- Own dependency wiring via DI container/factory.
- Handle runtime mode flags (paper/shadow/live), heartbeats, and health checks.

**Public APIs (pseudocode)**
```python
class AgentOrchestrator:
    def tick(self, now: datetime) -> TickResult
    def run_forever(self) -> None

class AgentApp:
    @classmethod
    def from_config(cls, cfg: AppConfig) -> "AgentApp"
    def start(self) -> None
    def stop(self) -> None
```

**Inputs / Outputs**
- Input: module services injected by interfaces.
- Output: deterministic `TickResult` records and persisted checkpoints.

---

## 3) Canonical state model

```python
@dataclass
class CanonicalState:
    as_of: datetime
    mode: Literal["paper", "shadow", "live"]
    runtime_flags: RuntimeFlags
    balances: dict[str, AssetBalance]               # key: asset
    positions: dict[str, Position]                  # key: symbol
    open_orders: dict[str, OrderRecord]             # key: client_order_id
    fills: deque[FillEvent]                         # bounded history for replay/audit
    risk_limits: RiskLimits
    pnl: PnLSnapshot
    treasury: TreasurySnapshot
    connectivity: ConnectivityState
    sequence: SequenceState

@dataclass
class RuntimeFlags:
    trading_enabled: bool
    kill_switch_active: bool
    pause_reason: str | None
    strategy_enabled: dict[str, bool]

@dataclass
class RiskLimits:
    max_notional_per_trade: Decimal
    max_position_notional_per_symbol: Decimal
    max_portfolio_exposure: Decimal
    daily_loss_limit: Decimal
    max_drawdown_limit: Decimal
    max_open_orders: int

@dataclass
class PnLSnapshot:
    realized: Decimal
    unrealized: Decimal
    fees: Decimal
    pnl_today: Decimal
    drawdown: Decimal

@dataclass
class TreasurySnapshot:
    principal_baseline: Decimal
    operating_costs_today: Decimal
    reserve_accrual_today: Decimal
    rolling_surplus_30d: Decimal
    principal_protected: bool
```

**State ownership rules**
- `portfolio/` owns canonical persistence and versioning.
- `execution/` is the only writer for `open_orders` transitions.
- `risk/` is the only writer for `runtime_flags.kill_switch_active` and risk breach markers.
- `agent/` performs transactional state updates per tick (read → decide → apply → persist).

---

## 4) Configuration strategy

### Typed schema
- Define `config/schema.py` using typed models (e.g., Pydantic/dataclasses):
  - `AppConfig`, `ExchangeConfig`, `StrategyConfig`, `RiskConfig`, `ExecutionConfig`, `TreasuryConfig`, `ObservabilityConfig`.
- Load order: `base.yaml` → `<env>.yaml` override → environment variables override.

### Environment separation
- `dev`: local replay/paper defaults, low-risk limits.
- `stage`: shadow/live-market-read, dry-run execution, production-like limits.
- `prod`: live execution, strict risk limits, mandatory alert routing.

### Secrets handling
- No secrets in repo, prompts, or config files.
- Secrets only via runtime environment injection or secret manager references.
- Validate required secrets at startup and fail fast if missing.
- Redact secret-like values from logs automatically.

### DI and global-state rules
- `agent/app.py` composes services from interfaces + config.
- Module internals receive dependencies via constructors.
- Ban mutable module-level singletons for runtime state.

---

## 5) Logging + metrics + alerts plan

### Structured logging
- JSON logs with required fields:
  - `ts`, `level`, `service`, `env`, `symbol`, `event_type`, `trace_id`, `tick_id`, `client_order_id`, `exchange_order_id`, `strategy`, `message`.
- Log categories:
  - `market_data`, `signal_decision`, `risk_decision`, `order_submit`, `order_update`, `reconcile`, `treasury`, `control_action`, `alert`.

### Key metrics
- **Data quality:** `md_latency_ms`, `md_staleness_s`, `ws_reconnect_count`.
- **Signal:** `signals_generated_total`, `signals_rejected_total{reason}`.
- **Risk:** `risk_reject_total{code}`, `daily_loss_pct`, `drawdown_pct`, `kill_switch_active` (gauge).
- **Execution:** `order_submit_total{status}`, `order_retry_total`, `fill_ratio`, `slippage_bps`, `reconcile_drift_count`.
- **Portfolio/Treasury:** `realized_pnl`, `fees_paid`, `operating_costs`, `rolling_surplus_30d`, `principal_protected`.
- **Runtime:** `tick_duration_ms`, `tick_failures_total`, `heartbeat_lag_ms`.

### Alert triggers (minimum)
1. Kill-switch activation (critical).
2. Daily loss limit breach (critical).
3. Drawdown breach (critical).
4. Market data staleness above threshold for N seconds (high).
5. Consecutive order rejects/retries above threshold (high).
6. Reconciliation drift unresolved for >M cycles (high).
7. Rolling 30-day surplus < 0 (medium/high).
8. Heartbeat missing for >2 intervals (critical).

---

## 6) Testing strategy

### Unit tests
- Pure logic tests for:
  - signal generation and validation,
  - risk policy math (daily loss, drawdown, exposure),
  - idempotency key semantics,
  - PnL and treasury surplus formulas,
  - config parsing and schema validation.
- No external I/O in unit tests.

### Integration tests
- Contract tests against `ExchangeAdapter` interface with deterministic fixtures.
- Orchestrator pipeline tests over single/multi-tick flows.
- Restart/recovery tests: persist state, restart, reconcile, confirm idempotent continuation.

### Sandbox / paper / shadow tests
- `sandbox/` E2E tests run in paper mode and shadow mode with controlled fixtures.
- Validate operator controls: pause/resume and kill-switch action path.
- Validate daily loss guard by synthetic loss scenario.

### Deterministic replay
- Replay engine feeds recorded `MarketEvent` streams with fixed seed and deterministic clock.
- Assertions:
  - identical input stream + config + seed => identical signals, risk decisions, and order intents.
- Store canonical replay fixtures under `tests/replay/fixtures/`.

### Mock exchange
- `execution/mock_exchange_adapter.py` emulates:
  - partial fills, delayed acks, rejects, cancels, stale snapshots, and transient errors.
- Required for retry/idempotency safety validation under failure modes.

### Idempotency and retry safety invariants (must hold)
1. Repeated `submit(intent, same_idem_key)` never creates duplicate live orders.
2. Retry after timeout produces at-most-once effective order placement.
3. Reconciliation resolves uncertain submission states to exchange truth deterministically.
4. Kill-switch path is safe under repeated invocation.

---

## Architecture constraints checklist (enforced)
- Dependency injection is mandatory across module boundaries.
- No global mutable runtime state.
- Execution is idempotent under retries and restarts.
- Kill-switch and daily loss limits are hard gates, not advisory signals.
- Python layer (`agent/` + domain modules) is the canonical decision source; exchange is external execution sink.
