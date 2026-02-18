# Strategy / Agent Decision Layer Audit

## Decision pipeline

### 1) Where decisions are generated

The repo currently uses **rule-based deterministic strategies**, not ML/LLM inference, in the live decision path.

- Stage 3 strategy generator:
  - `ProfitAwareStrategyV1.generate_intents(...)` in `strategies/profit_v1.py`.
  - Logic: take-profit sells when bid exceeds avg-cost by `min_profit_bps`; conservative buys under spread and balance constraints.
- Stage 4/5 decision layer:
  - `DecisionPipelineService` orchestrates universe selection, strategy intents, allocation sizing, and order request mapping.
  - Default registry strategy is `BaselineMeanReversionStrategy` (mean-reversion threshold vs anchor price).
  - Optional aggressive path uses momentum-like `aggressive_scores` from dynamic universe service and allocates by normalized weights.
- Stage 7 path:
  - `Stage7CycleRunner` composes universe selection -> portfolio policy plan -> order builder -> OMS simulation.
  - Still rules/policy driven; no learned model scoring in strategy core.

There is no runtime OpenAI/LLM usage in the decision stack.

### 2) Inputs to decision functions

Across stages, inputs include:

- Market microstructure/price:
  - orderbook best bid/ask, mark prices, spread, optional candle/ticker fallback in Stage7.
- Portfolio/account state:
  - balances (TRY and assets), positions (qty/avg cost), open orders, cash reserve targets.
- Universe metadata:
  - exchange pair info/rules, symbol eligibility filters.
- Strategy knobs/config:
  - threshold bps, max notional, bootstrap notional, fee buffer, cycle caps.
- Risk telemetry:
  - drawdown ratio, daily PnL, stale data age, spread spikes, quote volume, loss streak policy inputs.

Notably absent from current strategy inputs:
- news/sentiment feeds,
- alternative data,
- model-predicted returns.

### 3) How risk management is applied

Risk is layered:

1. **Pre-decision budgeting / sizing controls**
   - Allocation/portfolio policy enforces:
     - TRY cash reserve target (`try_cash_target`), optional TRY max,
     - per-symbol position cap,
     - per-cycle notional cap,
     - min order notional,
     - max orders per cycle.

2. **Stage 3 risk policy filters**
   - max open orders per symbol,
   - cooldown by symbol+side,
   - per-order notional cap,
   - cycle notional cap,
   - investable cash / reserve target gating.

3. **Stage 4 risk policy on lifecycle actions**
   - max open orders,
   - max position notional,
   - max daily loss,
   - max drawdown,
   - sell min-profit threshold (fee + slippage + min profit bps).

4. **Stage 7 risk-budget mode switching**
   - rule engine outputs `NORMAL`, `REDUCE_RISK_ONLY`, or `OBSERVE_ONLY` based on:
     - max drawdown breach,
     - max daily loss breach,
     - consecutive loss guardrail,
     - stale market data,
     - spread/quote-volume liquidity guardrails,
     - cooldown carry-over.

5. **Execution safety gates**
   - kill switch, dry-run, live-arm acknowledgment, safe-mode/observe-only behavior.

### 4) Self-funding interpretation (current)

Current behavior is **cash-reserve constrained reinvestment**, not an explicit treasury wallet model.

- Compounding mechanics today:
  - Equity/PnL updates change free TRY and position values in snapshots/ledger.
  - Future deploy budget is derived from current cash/equity minus reserve floor.
  - Therefore profits naturally increase deployable budget (after reserve constraints), i.e., implicit compounding.

- Reserve behavior today:
  - `try_cash_target` acts as a protected liquidity floor.
  - In portfolio policy/allocation, investable capital is `max(0, cash - cash_target)` (and related knobs).

- What is missing for explicit “self-funding” semantics:
  - dedicated treasury/reserve account with transfer rules,
  - explicit reinvestment ratio (e.g., 70% reinvest / 30% treasury sweep),
  - drawdown-aware compounding throttle.

### 5) Proposed clean separation (target architecture)

A clean layering for strategy-agent behavior should be:

1. **Data ingestion layer**
   - Typed market/account feeds (`MarketSnapshot`, `PortfolioSnapshot`) with timestamps and freshness metadata.

2. **Feature/indicator layer**
   - Deterministic feature calculators (spreads, momentum, volatility, inventory pressure, liquidity scores).
   - No order side effects.

3. **Decision layer (strategy policy)**
   - Pure function: `features + portfolio_state + knobs -> proposed intents`.
   - Strategy registry and composition live here.

4. **Risk layer**
   - Pure policy engine that transforms/rejects intents and emits risk rationale.
   - Includes budget, concentration, drawdown, and mode overrides.

5. **Execution planning layer**
   - Intent -> concrete orders (quantization, min notional, idempotency keys, client IDs).

6. **Execution adapter layer**
   - Exchange IO only (submit/cancel/reconcile/retry/rate-limit).

7. **State/ledger layer**
   - Event sourcing, accounting, PnL, replay/parity outputs.

### 6) Non-determinism sources + reproducibility plan

Key non-determinism sources observed:

- Time-based values (`datetime.now`, runtime timestamps, cooldown clocks).
- Randomized retry jitter / loop jitter.
- UUID generation for cycle/run IDs and intent IDs in some paths.
- Live external data timing/order and partial data availability.
- Dynamic universe refresh windows and freshness checks.

Backtest/replay reproducibility improvements:

1. Inject deterministic clocks (`now_provider`) everywhere in decision/risk/allocation services.
2. Replace random jitter with seeded PRNG controlled from run config.
3. Use deterministic IDs derived from `(cycle_ts, symbol, side, intent payload hash)` for backtests.
4. Snapshot and version all effective parameters/weights each cycle.
5. Enforce stable sorting before any iteration over symbols/intents/actions.
6. Run strategy/risk in pure mode against immutable snapshots; ban direct adapter calls from decision layer.
7. Persist full input envelopes for each cycle (market snapshot hash, portfolio hash, config hash).

## Risk/position sizing model (concise)

- Position sizing is not Kelly/ML-optimized; it is cap/budget constrained.
- Primary sizing gates:
  - cash reserve target,
  - fee buffer,
  - per-intent/per-cycle caps,
  - per-symbol exposure cap,
  - max orders per cycle,
  - min-notional and precision constraints.
- Stage7 overlays add mode-level throttling (`REDUCE_RISK_ONLY` and `OBSERVE_ONLY`) under risk stress.

## Self-funding mechanism (current + proposed)

### Current

- Implicit reinvestment via available cash/equity updates.
- Reserve floor via `try_cash_target`; deploy only above reserve.
- No explicit treasury allocation policy.

### Proposed

- Add `TreasuryPolicy` with explicit knobs:
  - `reinvest_ratio` (e.g., 0.7),
  - `treasury_sweep_ratio` (e.g., 0.3),
  - `max_reinvest_drawdown_gate` (reduce reinvestment during drawdown),
  - `min_treasury_balance_try`.
- Integrate into allocation step so deploy budget becomes:
  - `deploy_budget = max(0, reinvest_ratio * (equity - reserve - treasury_holdback))`.

## Refactor targets for clean layering

1. Split `Stage7CycleRunner` orchestration from decision logic into pure services.
2. Ensure `DecisionPipelineService` only consumes typed snapshots; no implicit fallback data fetches.
3. Merge Stage3/4/7 risk policy outputs under one normalized `RiskDecisionEnvelope` schema.
4. Standardize strategy interface across Stage3 and Stage5 (`Intent` model parity).
5. Introduce a dedicated feature store module (deterministic indicators + hashes).
6. Gate all backtest runs behind deterministic mode (seed, fixed clock, stable IDs, frozen params).
