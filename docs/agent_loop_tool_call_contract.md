# Agent Loop & Tool-Call Contract (Planner ↔ Python Executor)

## 1) Agent loop diagram (text)

```text
[START]
  -> (S1) PLAN
      Inputs: canonical_state, market_snapshot, config, prior_audit_context
      Output: plan_bundle
      Audit: record PLAN_CREATED
  -> (S2) VALIDATE
      Checks: risk, runway/budget, exposure, leverage, market sanity, policy
      Output: validation_report {pass|fail, reasons}
      Audit: record VALIDATION_RESULT
      If fail:
         -> (F1) REJECT_ACTION
             Output: no-op decision, remediation hints
             Audit: record ACTION_REJECTED
             -> (S7) RECORD
      If pass:
         -> (S3) DRY-RUN
             Simulate order sizing, fees/slippage, post-trade limits, idempotency collisions
             Output: dry_run_report {pass|fail, expected_outcome}
             Audit: record DRY_RUN_RESULT
             If fail:
                -> (F2) PLAN_ADJUST_OR_ABORT
                    Output: adjusted plan OR no-op
                    Audit: record DRY_RUN_ABORTED_OR_ADJUSTED
                    -> If adjusted: return to (S2) VALIDATE
                    -> If aborted: go to (S7) RECORD
             If pass:
                -> (S4) EXECUTE
                    Submit approved intents via Python executor
                    Output: execution_report (submitted/acked/rejected/unknown)
                    Audit: record EXECUTION_ATTEMPT
                -> (S5) VERIFY
                    Reconcile exchange truth vs intended actions
                    Output: verification_report (matched/drift/uncertain)
                    Audit: record VERIFICATION_RESULT
                    If uncertain/drift:
                       -> (F3) SAFE_RECOVERY
                           Actions: halt new entries, reconcile loop, optional kill-switch
                           Audit: record SAFE_RECOVERY_TRIGGERED
                -> (S6) RECORD
                    Persist decision, state delta, metrics, and ledger updates
                    Audit: record DECISION_RECORDED
                -> [END TICK]

Transitions for repeated loop:
[END TICK] -> next tick PLAN (heartbeat/event-driven)

Global failure branch (any step):
  error -> policy handler -> kill-switch consideration -> audit ERROR_EVENT -> RECORD
```

**Audit invariant:** every state transition above emits an immutable audit event with `decision_id`, `tick_id`, `timestamp`, `actor="codex|python"`, and `status`.

---

## 2) Tool-call contract (planner-callable Python functions)

### 2.1 Contract conventions
- Planner (Codex) can only call allowlisted Python functions below.
- All requests must include `decision_id`, `tick_id`, and `idempotency_key`.
- All responses must include `status`, `errors[]`, and `audit_ref`.

### 2.2 Function list + strict schemas

#### A) `py_get_canonical_state`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "as_of": "RFC3339 timestamp"
}
```
**Returns**
```json
{
  "status": "ok|error",
  "state": {
    "balances": [{"asset": "string", "free": "decimal", "locked": "decimal"}],
    "positions": [{"symbol": "string", "qty": "decimal", "avg_price": "decimal", "notional": "decimal"}],
    "open_orders": [{"order_client_id": "string", "symbol": "string", "side": "buy|sell", "qty": "decimal", "price": "decimal|null", "state": "new|partially_filled|filled|canceled|rejected"}],
    "risk_limits": {"daily_loss_limit": "decimal", "max_drawdown": "decimal", "max_notional_per_trade": "decimal", "max_portfolio_exposure": "decimal", "max_leverage": "decimal"},
    "runtime_flags": {"trading_enabled": "bool", "kill_switch_active": "bool", "mode": "paper|shadow|live"},
    "pnl": {"realized": "decimal", "unrealized": "decimal", "pnl_today": "decimal", "fees_today": "decimal"},
    "treasury": {"principal_baseline": "decimal", "rolling_surplus_30d": "decimal", "runway_days": "number"}
  },
  "errors": [],
  "audit_ref": "string"
}
```

#### B) `py_get_market_snapshot`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "symbols": ["string"],
  "max_staleness_sec": "number"
}
```
**Returns**
```json
{
  "status": "ok|error",
  "market": [{"symbol": "string", "best_bid": "decimal", "best_ask": "decimal", "mid": "decimal", "spread_bps": "number", "vol_1m": "number", "staleness_sec": "number", "halted": "bool"}],
  "errors": [],
  "audit_ref": "string"
}
```

#### C) `py_validate_plan`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "plan": {
    "intents": [{"symbol": "string", "side": "buy|sell", "order_type": "market|limit", "qty": "decimal", "limit_price": "decimal|null", "time_in_force": "GTC|IOC|FOK", "strategy_id": "string", "expected_edge_bps": "number"}],
    "reason": "string"
  }
}
```
**Returns**
```json
{
  "status": "ok|error",
  "validation": {
    "pass": "bool",
    "checks": [
      {"name": "risk_limits", "pass": "bool", "detail": "string"},
      {"name": "runway_budget", "pass": "bool", "detail": "string"},
      {"name": "exposure_leverage", "pass": "bool", "detail": "string"},
      {"name": "market_sanity", "pass": "bool", "detail": "string"},
      {"name": "policy_guardrails", "pass": "bool", "detail": "string"}
    ],
    "blocking_reasons": ["string"]
  },
  "errors": [],
  "audit_ref": "string"
}
```

#### D) `py_dry_run_plan`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "plan": {"intents": [{"symbol": "string", "side": "buy|sell", "order_type": "market|limit", "qty": "decimal", "limit_price": "decimal|null"}]},
  "assumptions": {"slippage_bps": "number", "fee_bps": "number"}
}
```
**Returns**
```json
{
  "status": "ok|error",
  "dry_run": {
    "pass": "bool",
    "projected": [{"symbol": "string", "est_fill_price": "decimal", "est_fee": "decimal", "est_pnl_impact": "decimal", "post_trade_exposure": "decimal"}],
    "idempotency_conflicts": ["order_client_id"],
    "blocking_reasons": ["string"]
  },
  "errors": [],
  "audit_ref": "string"
}
```

#### E) `py_execute_plan`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "execution_mode": "paper|shadow|live",
  "approved_intents": [{"intent_id": "string", "symbol": "string", "side": "buy|sell", "order_type": "market|limit", "qty": "decimal", "limit_price": "decimal|null", "time_in_force": "GTC|IOC|FOK", "order_client_id": "string"}]
}
```
**Returns**
```json
{
  "status": "ok|partial|error",
  "execution": [{"intent_id": "string", "order_client_id": "string", "exchange_order_id": "string|null", "submit_status": "acked|rejected|timeout|unknown", "reject_code": "string|null"}],
  "errors": [],
  "audit_ref": "string"
}
```

#### F) `py_verify_execution`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "order_client_ids": ["string"]
}
```
**Returns**
```json
{
  "status": "ok|error",
  "verification": {
    "overall": "matched|drift|uncertain",
    "orders": [{"order_client_id": "string", "exchange_order_id": "string|null", "final_state": "new|partially_filled|filled|canceled|rejected|unknown", "fill_qty": "decimal", "avg_fill_price": "decimal|null"}],
    "drift_reasons": ["string"]
  },
  "errors": [],
  "audit_ref": "string"
}
```

#### G) `py_record_decision`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "record": {
    "plan_summary": "string",
    "validation_summary": "string",
    "execution_summary": "string",
    "verification_summary": "string",
    "state_delta": "object",
    "metrics": "object"
  }
}
```
**Returns**
```json
{
  "status": "ok|error",
  "ledger_ref": "string",
  "audit_ref": "string",
  "errors": []
}
```

#### H) `py_set_kill_switch`
**Required params**
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "idempotency_key": "string",
  "active": "bool",
  "reason": "string",
  "actor": "string"
}
```
**Returns**
```json
{
  "status": "ok|error",
  "kill_switch_active": "bool",
  "errors": [],
  "audit_ref": "string"
}
```

---

## 3) Validation gates (mandatory before execute)

1. **Risk checks**
   - Daily realized loss must remain above `-daily_loss_limit` after projected trade.
   - Projected drawdown must remain within `max_drawdown`.
   - Per-trade notional must be <= `max_notional_per_trade`.
   - Reject if any breach is projected.

2. **Budget/runway checks**
   - Compute projected operating surplus after estimated fees + operating cost accrual.
   - Compute runway days (see Section 4).
   - Reject if runway below minimum threshold or rolling surplus policy is violated.

3. **Exposure/leverage limits**
   - Post-trade per-symbol and portfolio exposure must remain below configured limits.
   - Effective leverage must remain <= configured `max_leverage` (v1 expected 1.0).

4. **Market condition sanity checks**
   - Data freshness <= `max_staleness_sec`.
   - Spread and volatility inside strategy-specific guardrails.
   - Trading halt/maintenance flags must be false.
   - Reject if market microstructure indicates abnormal conditions (e.g., spread spike > threshold).

5. **Policy and runtime checks**
   - Trading must be enabled, kill-switch inactive, and mode-appropriate permissions valid.
   - Required audit fields present (`decision_id`, `tick_id`, `idempotency_key`).

**Gate outcome rule:** execution only allowed when all validation gates pass.

---

## 4) Self-financing logic

### 4.1 Cost model inputs
```json
{
  "trading_fees": "decimal/day",
  "infra_cost": "decimal/day",
  "data_cost": "decimal/day",
  "ops_overhead": "decimal/day",
  "reserve_accrual_rate": "number (0..1) on realized gains",
  "principal_baseline": "decimal",
  "current_equity": "decimal",
  "realized_pnl_rolling_30d": "decimal"
}
```

### 4.2 Core computations
- `daily_total_cost = trading_fees + infra_cost + data_cost + ops_overhead + reserve_accrual`
- `rolling_surplus_30d = realized_pnl_rolling_30d - rolling_total_cost_30d`
- `principal_protected = current_equity >= principal_baseline`
- `runway_days = cash_buffer_for_costs / max(daily_total_cost, epsilon)`

### 4.3 Risk scaling rules (capital protection first)
1. If `rolling_surplus_30d >= target_surplus` and all risk metrics healthy: normal risk budget.
2. If `0 <= rolling_surplus_30d < target_surplus`: reduce position sizing by configured factor (e.g., 0.75x).
3. If `rolling_surplus_30d < 0` OR `runway_days < min_runway_days`: reduce to defensive sizing (e.g., 0.25x) and disable new high-volatility symbols.
4. If `principal_protected == false` OR daily loss breach: stop opening new positions immediately.

### 4.4 Mandatory stop-trading conditions
- Kill-switch active.
- Daily loss limit reached/exceeded.
- Drawdown breach.
- Runway below hard minimum.
- Unresolved verification drift/uncertain order state beyond allowed reconciliation cycles.

All stop conditions emit critical audit events and alerts.

---

## 5) Idempotency + replay correlation model

### 5.1 Correlation identifiers
- `decision_id`: unique planner decision for a tick.
- `tick_id`: loop iteration identifier.
- `intent_id`: unique intent within decision.
- `order_client_id`: deterministic order id derived from `(decision_id, intent_id, symbol, side, qty, limit_price)`.
- `idempotency_key`: request-level retry key for Python calls.
- `exchange_order_id`: venue-provided id.

### 5.2 Idempotency rules
1. Same `idempotency_key` + same request payload must return same logical result.
2. Same `order_client_id` must map to at most one live exchange order.
3. Unknown submit outcomes require verification/reconciliation before re-submit.
4. Replay mode forbids non-deterministic clocks and random seeds unless fixed and recorded.

### 5.3 Replay record format
```json
{
  "decision_id": "string",
  "tick_id": "string",
  "inputs": {"state_hash": "string", "market_hash": "string", "config_hash": "string"},
  "plan": "object",
  "validation": "object",
  "dry_run": "object",
  "execution": "object",
  "verification": "object",
  "outputs": {"state_delta_hash": "string", "metrics": "object"},
  "timestamps": {"planned_at": "RFC3339", "executed_at": "RFC3339"}
}
```

**Replay invariant:** identical `inputs` + identical deterministic policy must produce identical `plan/validation/execution-intent` artifacts.

---

## 6) Safety policies

### 6.1 The agent must NEVER
1. Trade when kill-switch is active.
2. Bypass validation gates or execute with failed gate results.
3. Exceed configured leverage/exposure/daily-loss/drawdown limits.
4. Place withdrawal/transfer actions from runtime trading credentials.
5. Submit orders without `decision_id`, `tick_id`, `order_client_id`, and audit record.
6. Continue opening new positions under unresolved uncertain order state.
7. Modify risk limits autonomously outside approved policy ranges.
8. Log secrets or sensitive credentials.

### 6.2 Requires human approval
1. Enabling/disabling live mode.
2. Changing hard risk limits (daily loss, drawdown, leverage, exposure caps).
3. Deactivating kill-switch after critical event.
4. Treasury transfers above configured threshold.
5. Strategy family changes or new symbol universe additions.
6. Any override that suppresses critical alerts.

### 6.3 Audit requirement for all actions
Every planner and executor action must create an audit record with minimum schema:
```json
{
  "audit_id": "string",
  "decision_id": "string",
  "tick_id": "string",
  "step": "PLAN|VALIDATE|DRY_RUN|EXECUTE|VERIFY|RECORD|CONTROL",
  "actor": "codex|python|human",
  "status": "started|passed|failed|skipped",
  "reason": "string",
  "artifacts": ["uri-or-id"],
  "timestamp": "RFC3339"
}
```

This is mandatory and non-optional for production operation.
