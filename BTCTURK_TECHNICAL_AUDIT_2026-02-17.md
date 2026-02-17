Strategy Overview (inputs -> logic -> outputs)

- Stage3 runtime sequencing (from CLI orchestration):
  - Inputs: balances, orderbook bids/asks, persisted positions/fills/open orders, settings knobs.
  - Logic chain: `AccountingService.refresh` -> `StrategyService.generate` -> `RiskService.filter` -> `ExecutionService.execute_intents`.
  - Outputs: approved intents submitted/simulated as orders; state persisted to SQLite.

- Signal generation (Stage3 default strategy: `ProfitAwareStrategyV1`):
  - Inputs:
    - per-symbol top-of-book `bid/ask` from `MarketDataService`.
    - current position (`qty`, `avg_cost`) from accounting state.
    - TRY free balance from portfolio balances.
    - `min_profit_bps`, `ttl_seconds` from settings.
  - Logic:
    - If position exists and `bid >= avg_cost * (1 + min_profit_bps/10000)`, emit SELL intent for 25% of position (`take_profit`).
    - Else, if no position and spread `(ask-bid)/bid <= 1%`, emit BUY intent using `budget=min(TRY_free, 100)` (`conservative_entry`).
  - Outputs:
    - `Intent` list (`symbol`, `side`, `qty`, `limit_price`, `ttl_seconds`, idempotency key).

- Indicator/data computation and timeframes:
  - Stage3 strategy uses only instantaneous best bid/ask and current position cost basis.
  - No candle/feature window (EMA/RSI/VWAP) in `ProfitAwareStrategyV1`.
  - Implicit timeframe: cycle interval (`--cycle-seconds`) and data freshness guard (`max_market_data_age_ms`).

- Lookahead bias risk assessment:
  - No backtest label leakage in live cycle path (uses current snapshot only).
  - Risks remain:
    - Decision and execution occur in same cycle using one snapshot -> stale/latency slippage risk.
    - Stage3 dry-run can overestimate fill quality (no orderbook depth/queue-position model in core stage3 flow).

- Separation of concerns (current):
  - Strategy: intent generation (`strategies/`, `StrategyService`).
  - Risk: intent gating/capping (`risk/policy.py`, `services/risk_service.py`; Stage4 has separate `services/risk_policy.py`).
  - Execution: submit/cancel/reconcile/idempotency (`services/execution_service.py`).
  - Portfolio/accounting: fills->positions/PnL and ledger snapshots (`accounting/accounting_service.py`, `services/ledger_service.py`).
  - This split is mostly clean; coupling exists through shared settings/state-store and stage multiplexing.

Risk Controls Inventory (table: control, threshold, enforcement location)

| Control | Threshold / Rule | Enforcement location |
|---|---|---|
| Live-side-effect arm gate | Requires `DRY_RUN=false`, `KILL_SWITCH=false`, `LIVE_TRADING=true`, ACK | `cli.run_cycle` + `services/trading_policy.py` + settings validators |
| Safe mode | Observe-only when `SAFE_MODE=true` | `cli.run_cycle`, `ExecutionService.execute_intents` |
| Kill switch | Blocks submit/cancel when enabled | `ExecutionService.execute_intents`, `cancel_stale_orders` |
| Max orders/cycle (Stage3) | `MAX_ORDERS_PER_CYCLE` | `risk/policy.py::evaluate` |
| Max open orders/symbol (Stage3) | `MAX_OPEN_ORDERS_PER_SYMBOL` | `risk/policy.py::evaluate` |
| Cooldown per symbol/side | `COOLDOWN_SECONDS` | `risk/policy.py::evaluate` |
| Min notional / tick / step normalization | exchange rules quantization + min notional block | `risk/policy.py::_normalize_intent`, `domain/models.py` |
| Cash reserve target | block when used_notional exceeds investable budget | `risk/policy.py::evaluate` (cash_reserve_target) |
| Max notional per order | cap qty by `MAX_NOTIONAL_PER_ORDER_TRY` | `risk/policy.py::evaluate` |
| Cycle notional cap | block when cumulative notional exceeds `NOTIONAL_CAP_TRY_PER_CYCLE` | `risk/policy.py::evaluate` |
| Market data freshness fail-closed | stale/missing/WS-disconnected => block cycle | `cli.run_cycle` + `services/market_data_service.py` |
| Stage4 daily loss guard | `realized_today_try <= -MAX_DAILY_LOSS_TRY` => reject all | `services/risk_policy.py::filter_actions` |
| Stage4 drawdown guard | `drawdown_pct >= MAX_DRAWDOWN_PCT` => reject all | `services/risk_policy.py::filter_actions` |
| Stage4 max position notional | projected position notional cap | `services/risk_policy.py::filter_actions` |
| Stage4 min profit for sells | required price includes fees+slippage+profit bps | `services/risk_policy.py::filter_actions` |

Self-Funding Accounting Model (explicit formulas)

- Capital availability / deployable budget (allocation layer):
  - `investable_total_try = max(0, cash_try - target_try_cash)`
  - `fee_buffer_ratio = max(fee_buffer_ratio, fee_buffer_bps/10000)`
  - `deploy_budget_try = investable_this_cycle_try / (1 + fee_buffer_ratio)`
  - with optional caps from `investable_usage_mode`, `max_try_per_cycle`, `try_cash_max`.

- Position accounting (Stage3 accounting service):
  - BUY fill update:
    - `total_cost = old_qty*old_avg_cost + fill_qty*fill_price + fee_quote`
    - `new_avg_cost = total_cost / new_qty`
  - SELL fill realized PnL:
    - `sell_qty = min(position_qty, fill_qty)`
    - `fee_used = fee_quote * (sell_qty/fill_qty)`
    - `realized_pnl += sell_qty*(fill_price - avg_cost) - fee_used`

- Mark-to-market and net PnL (ledger service):
  - `unrealized_pnl = Σ_lots (mark - unit_cost) * qty`
  - `realized_pnl = Σ symbol realized_pnl` (from matched fills/events)
  - `gross_pnl_try = realized_pnl + unrealized_pnl`
  - `net_pnl_try = gross_pnl_try - fees_try - slippage_try`
  - `equity_try = cash_try + position_mtm_try`

- Self-funding interpretation in this repo:
  - “Self-funding” is implemented as cash-reserve + investable-budget policy and PnL accumulation in state, not an external treasury transfer engine.
  - Profits become deployable indirectly through increased cash/equity subject to `TRY_CASH_TARGET`, per-cycle notional caps, and risk gates.
  - No explicit automatic profit sweep/withdrawal/segregated treasury transfer flow was found in Stage3 runtime path.

Vulnerabilities (bias, overtrading, missing constraints)

1. Signal simplicity / regime fragility
- One-snapshot spread + avg-cost rule; no volatility or trend regime filter in Stage3 default strategy.

2. Potential overtrading under choppy microstructure
- Conservative entry can repeatedly re-open positions each cycle if spread stays <=1% and cooldown/open-order checks permit.

3. Lookahead/quality mismatch in dry-run expectations
- Dry-run/live planning can assume executable top-of-book prices; depth and queue-position effects are not explicitly modeled in Stage3 strategy logic.

4. Fee currency treatment gap
- Accounting ignores non-quote fee currency in Stage3 accounting service (warns and skips conversion), which can distort realized/net PnL.

5. Risk-model split complexity
- Stage3 and Stage4 risk policies differ materially (intent gating vs lifecycle-action gating), increasing operator misconfiguration risk if stage context is unclear.

6. Missing explicit leverage controls
- Spot bot context implies no margin/leverage path by default, but there is no standalone “leverage must be zero” invariant check in strategy/risk boundary.

Refactor Plan (prioritized)

P0 (high impact, low/medium scope)
1. Add explicit fee-currency conversion pipeline in Stage3 accounting
- Convert non-quote fees via mark/FX provider before PnL updates instead of dropping them.
- Files: `accounting/accounting_service.py`, potentially `services/market_data_service.py`.

2. Add strategy-level anti-churn guardrails
- Add min hold time / re-entry cooldown / spread+volatility gate beyond static 1% spread check.
- Files: `strategies/profit_v1.py`, `services/strategy_service.py`, settings in `config.py`.

P1 (medium impact, medium scope)
3. Unify risk control semantics across stages
- Create shared risk contract (common control names + telemetry) and adapters for Stage3/4 to reduce drift.
- Files: `risk/policy.py`, `services/risk_policy.py`, `services/risk_service.py`.

4. Add explicit “self-funding policy” service
- Centralize formulas and states: reserve floor, compounding fraction, optional profit sweep account.
- Files: new `services/self_funding_policy.py`; integrate in allocation/risk.

P2 (medium impact, small/medium scope)
5. Add strategy/audit telemetry for effective constraints
- Emit per-cycle structured fields: deploy_budget_try, blocked_by_cash_target, blocked_by_cooldown, net_pnl components.
- Files: `cli.py`, `services/allocation_service.py`, `services/state_store.py`.

6. Add stronger no-lookahead and overtrading tests
- Deterministic tests for cycle-to-cycle signal behavior under stale/latency/slippage scenarios.
- Files: `tests/test_strategy_stage3.py`, `tests/test_risk_policy_stage3.py`, new scenario tests.
