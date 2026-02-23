# Project Intake & Specification Freeze: Self-Financing Crypto Agent Bot

## 1) Mission statement
Build a production-grade Python trading agent that is **self-financing**, meaning all recurring operating costs (infrastructure, exchange fees, and reserved tax/contingency accrual) are paid from realized trading profits while preserving or increasing starting equity over rolling 30-day periods.

## 2) Assumptions
1. **Exchange type:** Centralized exchange (CEX) with authenticated REST + WebSocket APIs; single venue in v1.
2. **Instruments:** Spot crypto pairs in v1 (e.g., BTC/USDT, ETH/USDT); no derivatives in initial release.
3. **Latency model:** Retail/API latency regime; decision frequency 1s–60s, not HFT.
4. **Capital base:** Initial base capital is fixed at deployment time and tracked as principal.
5. **Leverage:** 1x only in v1 (no borrowing, no margin, no perps).
6. **Fee model:** Maker/taker percentage fees plus withdrawal/network fees; fee tier treated as configurable input.
7. **Slippage model:** Conservative fixed + dynamic slippage assumption for pre-trade checks and backtests.
8. **Trading schedule:** 24/7 operation with configurable maintenance windows.
9. **Jurisdiction/tax handling:** Tax is modeled as reserve accrual percentage on realized PnL; tax filing is out of system scope.
10. **Cost accounting horizon:** Self-financing evaluation window defaults to 30 days and is configurable.

## 3) Functional requirements

### 3.1 Market data
1. Ingest real-time top-of-book/trade/candle streams from exchange WebSocket.
2. Backfill missing candles/orderbook snapshots via REST.
3. Enforce data quality gates: staleness thresholds, sequence integrity, and schema validation.
4. Persist normalized market data required for replay/backtesting and post-trade analysis.

### 3.2 Signal generation
1. Support pluggable strategy modules behind a common interface (`generate_signal(context)`).
2. Emit normalized signal payload: timestamp, instrument, direction, confidence, expected edge, expiry.
3. Reject signals when required features are stale/missing.
4. Support ensemble arbitration (single active strategy in v1, expandable to multi-strategy).

### 3.3 Risk management
1. Pre-trade checks must include: max position size, max notional per trade, max daily loss, max drawdown, and min expected edge after fees/slippage.
2. Real-time exposure controls: per-instrument cap and portfolio-level cap.
3. Circuit breakers: halt new entries on connectivity degradation, abnormal slippage, or breach of risk limits.
4. Kill switch: immediate cancel-open-orders and flatten-to-cash mode (best effort with audit trail).

### 3.4 Execution & order management
1. Translate approved intents into exchange-native orders (market/limit; post-only optional by venue capability).
2. Ensure idempotent order submission and safe retry semantics.
3. Track full order lifecycle (new, partially filled, filled, canceled, rejected, expired).
4. Reconcile local state with exchange truth at fixed intervals and on restart.
5. Maintain deterministic client order IDs for traceability.

### 3.5 Portfolio & treasury accounting
1. Maintain principal ledger, realized PnL ledger, unrealized PnL snapshot, and fee ledger.
2. Maintain operations-cost ledger (infra/API/subscriptions as configured entries).
3. Maintain tax/contingency reserve ledger as configurable % of realized gains.
4. Compute self-financing status daily and over rolling windows:
   - Net operating surplus = realized PnL - trading fees - operating costs - reserve accrual.
   - Principal protection status = current equity >= initial principal.
5. Enforce treasury policy:
   - If surplus > threshold, transfer amount to operations wallet/account bucket.
   - If surplus < 0 beyond tolerance, reduce risk budget or halt entries.

### 3.6 Agent loop & orchestration
1. Run deterministic loop stages: ingest → feature build → signal → risk gate → execution → reconcile → metrics.
2. Support event-driven operation with periodic heartbeat tasks.
3. Persist checkpoints so process restarts are state-consistent.
4. Provide mode flags: paper-trading, shadow-live, and live-trading.

### 3.7 Operations interfaces
1. Config-driven deployment (no hardcoded secrets or venue parameters).
2. Operator controls: pause/resume strategy, adjust risk budgets, trigger kill switch.
3. Human-readable daily report including PnL, costs, reserve, self-financing status, and limit breaches.

## 4) Non-functional requirements

### 4.1 Security
1. API keys stored in secret manager or encrypted env injection; never logged in plaintext.
2. Principle of least privilege for credentials (trade-only keys where possible; no withdrawal rights in bot runtime).
3. Signed requests and nonce/timestamp protections per exchange spec.
4. Immutable audit logs for operator actions and risk overrides.

### 4.2 Reliability
1. Target uptime: >=99.5% monthly for trading loop process.
2. Automatic reconnect with bounded exponential backoff for API/WS failures.
3. Graceful degradation: stop opening new positions when data integrity cannot be guaranteed.
4. Crash recovery: restart must rebuild state from persisted checkpoints + exchange reconciliation.

### 4.3 Observability
1. Structured logging (JSON) with correlation IDs (order_id, strategy_run_id, loop_tick_id).
2. Metrics for latency, fill rate, slippage, rejection rate, drawdown, and self-financing surplus.
3. Alerting on critical conditions: kill switch, risk breach, prolonged disconnect, data staleness, negative rolling surplus.

### 4.4 Testing & validation
1. Unit tests for all pure logic (signal transforms, risk math, fee and surplus calculations).
2. Integration tests for exchange adapters with deterministic fixtures.
3. Replay/backtest tests for strategy determinism and risk-policy enforcement.
4. Dry-run simulation gate required before enabling live mode.
5. Regression suite must pass before release.

### 4.5 Deployment & operations
1. Containerized deployment with pinned dependencies and reproducible builds.
2. Environment promotion path: local → staging (paper/shadow) → production live.
3. Versioned config and migration-safe state schema changes.
4. Rolling/blue-green deploy capability with health checks.

## 5) Explicit out-of-scope
1. Cross-exchange arbitrage and smart order routing across venues.
2. Derivatives trading (futures, options, perps), margin, and leverage >1x.
3. On-chain DEX execution, MEV-aware routing, or custody automation.
4. Autonomous strategy self-modification without operator approval.
5. Tax filing/report generation for legal submission.
6. High-frequency microsecond-level co-located execution.
7. Social trading/copy trading features.

## 6) Minimal Definition of Done (DoD) checklist
- [ ] Market data ingestion, validation, and persistence run continuously for target instruments.
- [ ] Signal engine emits normalized intents and rejects on stale features.
- [ ] Risk gate blocks orders that violate configured limits; tested for all critical constraints.
- [ ] Execution adapter submits/cancels/reconciles orders with idempotency guarantees.
- [ ] Portfolio/treasury ledgers compute daily and rolling self-financing status correctly.
- [ ] Kill switch and circuit breakers are tested in staging.
- [ ] Observability stack emits required logs, metrics, and alerts.
- [ ] End-to-end paper/shadow run completes for predefined burn-in period with no Sev-1 defects.
- [ ] Security checklist completed (secret handling, key scope, audit logging).
- [ ] Release checklist signed off with rollback plan documented.

## OPEN QUESTION items (max 10)
1. **OPEN QUESTION:** What exact trading venue(s) and account type will v1 target (e.g., Binance spot, Coinbase Advanced)?
   - **Why it matters:** API capabilities, fee tiers, and order semantics directly affect adapter and risk design.
2. **OPEN QUESTION:** What is the precise baseline principal amount and reporting currency (USDT/USD/TRY)?
   - **Why it matters:** Self-financing and drawdown thresholds are meaningless without principal and base currency.
3. **OPEN QUESTION:** What operating costs must be included in “self-financing” (compute only vs. compute+data+human oversight allowance)?
   - **Why it matters:** Scope of cost ledger determines mission success/failure criteria.
4. **OPEN QUESTION:** What rolling-window success threshold defines self-financing pass/fail (e.g., surplus >= 0 every 30 days, or 90% of windows)?
   - **Why it matters:** Needed for objective acceptance tests and alert policy.
5. **OPEN QUESTION:** What maximum acceptable drawdown and daily loss limits should be enforced at launch?
   - **Why it matters:** Core risk limits must be fixed before live capital deployment.
6. **OPEN QUESTION:** Which strategy family is approved for v1 (mean reversion, momentum, market making), and what holding horizon?
   - **Why it matters:** Drives data granularity, execution style, and expected slippage behavior.
7. **OPEN QUESTION:** What is the minimum required paper/shadow burn-in duration and success metrics before live enablement?
   - **Why it matters:** Defines objective go-live gate and reduces premature deployment risk.
8. **OPEN QUESTION:** What are the operator response SLAs for alerts (24/7 on-call vs business hours)?
   - **Why it matters:** Circuit breaker and auto-halt policies depend on human intervention assumptions.
9. **OPEN QUESTION:** Are transfers from trading account to operations reserve executed automatically or manual-approval only?
   - **Why it matters:** Affects treasury workflow, permissions, and compliance controls.
10. **OPEN QUESTION:** What compliance constraints apply (jurisdictional restrictions, KYC entity type, prohibited assets)?
    - **Why it matters:** Determines permitted instruments and deployment legality.
