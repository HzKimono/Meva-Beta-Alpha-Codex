# RISK, SELF-FUNDING, AND ACCOUNTING CORRECTNESS AUDIT

## 1) Position sizing logic (inputs, caps, leverage if any)

### 1.1 Stage 3 sizing path
- Strategy sizing starts in `src/btcbot/strategies/profit_v1.py::ProfitAwareStrategyV1.generate_intents`:
  - Sell sizing: if `bid >= avg_cost * (1 + min_profit_bps/10000)`, sells `25%` of current quantity.
  - Buy sizing: uses TRY free balance, budgets `min(TRY balance, 100)` and sets `qty = budget / ask`.
- Pre-trade caps are enforced in `src/btcbot/risk/policy.py::RiskPolicy.evaluate`:
  - `max_orders_per_cycle`,
  - `max_open_orders_per_symbol`,
  - cooldown by `(symbol, side)`,
  - `max_notional_per_order_try` (with qty down-cap),
  - cycle total `notional_cap_try_per_cycle`,
  - investable cash cap `investable_try` (computed in `src/btcbot/cli.py::run_cycle` as `max(0, cash_try_free - try_cash_target)`).
- Price/qty are quantized to exchange rules in `_normalize_intent` before notional checks.

### 1.2 Stage 4/Stage 5/Stage 7 sizing path
- Allocation sizing is centralized in `src/btcbot/services/allocation_service.py::AllocationService.allocate` using:
  - `target_try_cash`, `try_cash_max`,
  - `min_order_notional_try`,
  - `max_intent_notional_try`,
  - `max_position_try_per_symbol`,
  - `max_total_notional_try_per_cycle`,
  - fee buffer (`fee_buffer_bps` / `fee_buffer_ratio`),
  - investable usage policy (`investable_usage_mode`, `investable_usage_fraction`, `max_try_per_cycle`).
- Stage4 pre-submit risk checks are in `src/btcbot/services/risk_policy.py::RiskPolicy.filter_actions`:
  - `max_open_orders`,
  - `max_position_notional_try`,
  - min required profit on sells (fees + slippage + `min_profit_bps`).
- Stage7 budget multipliers are in `src/btcbot/risk/budget.py::RiskBudgetPolicy.evaluate`:
  - max exposure and max order are proportional to available capital,
  - multiplier reduced on loss streak / stressed volatility / risk mode.

### 1.3 Leverage
- No leveraged/margin position model is present in these paths.
- Exposure is treated as cash-notional caps; no explicit leverage parameter found in inspected risk policy code.

---

## 2) Loss limits (daily/weekly), max drawdown, circuit breakers, cooldowns

### 2.1 Daily and drawdown limits
- Stage4: `src/btcbot/services/risk_policy.py::RiskPolicy.filter_actions` blocks all actions if:
  - `pnl.realized_today_try <= -max_daily_loss_try`, or
  - `pnl.drawdown_pct >= max_drawdown_pct`.
- Stage7 risk mode: `src/btcbot/domain/risk_budget.py::decide_mode` returns `OBSERVE_ONLY` when:
  - `drawdown_try >= max_drawdown_try` or
  - `daily_pnl_try <= -max_daily_drawdown_try`.
- Stage7 self-financing budget policy in `risk/budget.py` also enforces halts by daily loss ratio and drawdown ratio.

### 2.2 Circuit-breaker / observe-only behavior
- Stage3 live side-effect circuit controls:
  - `src/btcbot/services/trading_policy.py::validate_live_side_effects_policy` (`KILL_SWITCH`, `DRY_RUN`, live-arming).
- Startup recovery circuit:
  - `src/btcbot/services/startup_recovery.py::StartupRecoveryService.run` sets observe-only requirement on invariant violations/missing marks.
- Agent guardrail circuit:
  - `src/btcbot/agent/guardrails.py::SafetyGuard.apply` forces observe-only on kill switch, safe mode, drawdown, stale data, cooldown, or max exposure triggers.

### 2.3 Cooldowns
- Stage3 cooldown: per `(symbol, side)` gate in `risk/policy.py::RiskPolicy.evaluate` using `last_intent_ts_by_symbol_side` and `cooldown_seconds`.
- Agent cooldown: `SafetyGuard.apply` checks `cooldown_until` in risk_state.

### 2.4 Weekly loss
- No explicit weekly loss limit enforcement found in the inspected risk-policy functions.

---

## 3) Stops/TP/trailing logic and where enforced (pre-trade vs post-trade)

### 3.1 Take-profit logic
- Explicit TP-style behavior exists in Stage3 strategy generation (`profit_v1.py::generate_intents`):
  - sells are generated only above min profit threshold.
- Stage4 risk layer also enforces sell profitability floor (`services/risk_policy.py::filter_actions`, reason `min_profit_threshold`).

### 3.2 Stop-loss and trailing stop
- No explicit stop-loss or trailing-stop order generation/enforcement was found in inspected modules.
- Protection instead relies on global risk gates (daily loss, drawdown, observe-only mode) rather than per-position hard stops.

### 3.3 Enforcement stage
- TP threshold is enforced **pre-trade** (intent/action filtering).
- Drawdown/daily-loss protection is also **pre-trade** (blocks actions/intents).
- No post-trade trailing/stop manager loop was found in inspected files.

---

## 4) Fee model + slippage assumptions

### 4.1 Fees in risk and sizing
- Stage4 risk policy includes `fee_bps_taker` in minimum required sell profitability.
- Allocation includes fee buffer via `fee_buffer_bps` / `fee_buffer_ratio` to preserve cash reserve after estimated fees.

### 4.2 Fees in accounting
- Stage3 accounting (`accounting/accounting_service.py::_apply_fill`) includes fees only when fee currency equals quote currency; non-quote fees are ignored with warning.
- Stage4 accounting service (`services/accounting_service_stage4.py::apply_fills`) similarly tracks TRY fees; non-TRY fees create audit notes (`fee_conversion_missing:*`).

### 4.3 Slippage assumptions
- Stage4 risk policy adds `slippage_bps_buffer` to min required sell price.
- Stage7 market simulator (`services/oms_service.py::Stage7MarketSimulator.fill_slices`) applies synthetic slippage by side using `stage7_slippage_bps`.

---

## 5) PnL calculation method (realized/unrealized), partial fills, average price

### 5.1 Stage3 accounting model
- Realized/unrealized are updated in `accounting/accounting_service.py`:
  - BUY: weighted-average cost update including quote-currency fee.
  - SELL: realized pnl on `sell_qty=min(position.qty, fill.qty)` with proportional fee allocation.
  - Position reset when qty <= 0.
  - Unrealized pnl = `(mark - avg_cost) * qty` if mark>0.
- Partial fills are naturally aggregated because fills are applied incrementally and idempotently (`save_fill` guard).

### 5.2 Stage4 accounting model
- `services/accounting_service_stage4.py::apply_fills`:
  - BUY adjusts avg cost and qty,
  - SELL computes realized pnl and reduces qty,
  - explicit oversell guard raises `AccountingIntegrityError`.
- Snapshot output includes equity, realized today, total realized, drawdown.

### 5.3 Deterministic ledger model
- `accounting/ledger.py::AccountingLedger.recompute` provides event-sourced deterministic accounting:
  - FIFO lot matching,
  - explicit oversell ValueError,
  - realized/unrealized/fees/funding/slippage totals,
  - dedupe by event_id with deterministic tie-break key.

---

## 6) Self-funding mechanics (profit allocation, compounding rules, capital changes over time)

### 6.1 Policy definition
- `src/btcbot/risk/budget.py::SelfFinancingPolicy` defines:
  - `profit_compound_ratio` (default 0.60),
  - `profit_treasury_ratio` (default 0.40),
  - risk-reduction multipliers and halt ratios.

### 6.2 Capital mutation rule
- `RiskBudgetPolicy.apply_self_financing`:
  - Positive realized delta: split between trading capital and treasury by the configured ratios.
  - Negative realized delta: applied fully to trading capital; treasury unchanged.

### 6.3 Compounding behavior over time
- Compounding is applied through increased `trading_capital_try`, then `RiskBudgetPolicy.evaluate` computes larger/smaller risk limits (`max_exposure`, `max_order`) as ratios of capital.
- Thus risk budget scales with profitable history and contracts with losses.

---

## 7) Invariants (must always hold) + test ideas

1. **No negative free balances after startup recovery checks.**
   - Enforced by: `StartupRecoveryService.run` invariant scan.
   - Test idea: Unit test with mocked `PortfolioService` returning negative balance; assert `observe_only_required=True` and invariant error contains `negative_balance:*`.

2. **No negative position quantities after startup recovery checks.**
   - Enforced by: `StartupRecoveryService.run` over `accounting_service.get_positions()`.
   - Test idea: Unit test with mocked negative qty position; assert observe-only forced.

3. **Approved intents must not exceed per-cycle notional cap.**
   - Enforced by: `risk/policy.py::RiskPolicy.evaluate` (`notional_cap`).
   - Test idea: Unit test with intents summing above cap; assert later intents rejected.

4. **Approved intent notional must not exceed investable cash reserve policy.**
   - Enforced by: `RiskPolicy.evaluate` (`cash_reserve_target`).
   - Test idea: Unit test with low `investable_try`; assert buy intents blocked.

5. **Open orders per symbol must remain <= configured maximum.**
   - Enforced by: `RiskPolicy.evaluate` (`max_open_orders_per_symbol`).
   - Test idea: Unit test with `open_orders_by_symbol` preloaded at max; assert reject.

6. **Cooldown must prevent rapid same-side repeats per symbol.**
   - Enforced by: `RiskPolicy.evaluate` cooldown branch.
   - Test idea: Unit test with `last_intent_ts_by_symbol_side` set near-now; assert reject.

7. **Actions must be blocked under max daily loss or drawdown breach (Stage4).**
   - Enforced by: `services/risk_policy.py::filter_actions` early returns.
   - Test idea: Unit test with PnL snapshot below loss threshold and high drawdown; assert all rejected reasons.

8. **Sell action price must clear fee+slippage+profit threshold when position exists (Stage4).**
   - Enforced by: `filter_actions` required_price check.
   - Test idea: Unit test with low sell price; assert `min_profit_threshold` rejection.

9. **Accounting must never allow oversell in Stage4 fill application.**
   - Enforced by: `AccountingService.apply_fills` raising `AccountingIntegrityError`.
   - Test idea: Integration test with position qty smaller than sell fill qty; assert exception and no corrupted state.

10. **Ledger replay must reject oversell and remain deterministic by event_id dedupe.**
    - Enforced by: `AccountingLedger.recompute` oversell ValueError and `_dedupe_events`.
    - Test idea: Unit test with duplicate event IDs and oversell sequence; assert deterministic dedupe then oversell error.

11. **Live side effects must not execute when kill switch/dry-run/live-arming gates fail.**
    - Enforced by: `trading_policy.validate_live_side_effects_policy` and callers.
    - Test idea: Matrix test over gate combinations asserting block reason and no submit/cancel calls.

12. **Agent guardrail must force observe-only on stale data / drawdown / max exposure.**
    - Enforced by: `SafetyGuard.apply` blocked reasons path.
    - Test idea: Unit tests per condition verifying returned decision action = `OBSERVE_ONLY`.

---

## 8) Edge-case checklist + coverage status (YES/NO)

| Edge case | Covered? | Evidence |
|---|---|---|
| Non-quote fee currency in Stage3 accounting | **YES (with caveat)** | Warns and ignores non-quote fee; not converted (`accounting_service.py::_apply_fill`). |
| Non-TRY fee in Stage4 accounting | **YES (with caveat)** | Records audit note `fee_conversion_missing`, fee not converted (`accounting_service_stage4.py::apply_fills`). |
| Oversell protection Stage4 | **YES** | Raises `AccountingIntegrityError` on sell qty > position qty (`apply_fills`). |
| Oversell protection deterministic ledger | **YES** | Raises ValueError during lot matching (`accounting/ledger.py::recompute`). |
| Missing mark prices at startup | **YES** | Recovery flags `missing_mark_prices` and observe-only (`startup_recovery.py::run`). |
| Cooldown enforcement per symbol+side | **YES** | `risk/policy.py::RiskPolicy.evaluate`. |
| Daily loss hard stop Stage4 | **YES** | `services/risk_policy.py::filter_actions`. |
| Drawdown hard stop Stage4 | **YES** | `services/risk_policy.py::filter_actions`. |
| Weekly loss cap | **NO** | No explicit weekly-limit condition in inspected risk policies. |
| Trailing stop logic | **NO** | No trailing-stop manager in inspected strategy/risk/execution paths. |
| Explicit stop-loss orders | **NO** | No dedicated stop-loss rule/order generation found. |
| Partial fill handling in Stage3 accounting | **YES** | fill-by-fill updates with sell_qty min and fee prorate (`_apply_fill`). |
| Dynamic risk scaling after gains/losses | **YES** | `RiskBudgetPolicy.apply_self_financing` + `evaluate` scaling. |
| Reconciliation SLA invariant (e.g., "within N seconds") | **NO** | Reconcile logic exists, but hard SLA threshold not encoded in inspected functions. |

---

## Key risk notes for follow-up
- Accounting fee conversion gap (non-quote/non-TRY fees ignored) can bias realized PnL and risk signals.
- Absence of explicit stop-loss/trailing-stop means risk relies on coarse global halts, not per-position risk exits.
- Weekly loss/SLA invariants are policy gaps if required by production risk governance.
