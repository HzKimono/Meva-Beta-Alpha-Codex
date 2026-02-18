# Trading Systems Safety Audit: Capital, Risk Stops, and Security Controls (Evidence-Only)

## Scope and method
- Scope analyzed: Stage 3 default runtime path (`btcbot run`) plus Stage 4/Stage 7 risk-budget and capital-policy modules where present in code.
- Evidence sources: configuration, risk/allocation/execution/accounting/security/adapters modules only; no assumptions beyond inspected files.

---

## A) Risk controls table

| Control | Location (file/function) | Prevents | Gaps / Unknowns |
|---|---|---|---|
| Live-side-effects gating (`DRY_RUN`, `KILL_SWITCH`, `LIVE_TRADING`, ACK) | `services/trading_policy.py::validate_live_side_effects_policy`, `cli.py::_compute_live_policy`, `cli.py::run_cycle` | Unarmed live order submit/cancel operations | Stage 3 enforces gating in code path inspected; no separate external policy engine found in these files. |
| Safe mode forces observe-only | `cli.py::_compute_live_policy`, `cli.py::run_cycle` | Any write-side effects when safety mode engaged | Stage 3 loop has no file-lock singleton (lock appears only in stage4/stage7 commands). |
| Startup invariant stop (observe-only escalation) | `services/startup_recovery.py::StartupRecoveryService.run`, `cli.py::run_cycle` | Trading after negative balances or negative position qty; trading with missing mark prices | Missing mark prices causes observe-only reason; does not itself guarantee external alerting beyond logs. |
| Per-cycle order count/open-order/cooldown caps | `risk/policy.py::RiskPolicy.evaluate` | Over-trading and repeated rapid intents on same symbol/side | No explicit leverage/margin controls found in this Stage 3 policy file. |
| Notional caps (per-order and per-cycle) | `risk/policy.py::RiskPolicy.evaluate`, config knobs in `config.py` | Excess order size and aggregate cycle notional overspend | Caps depend on correct mark/limit prices and exchange rules availability. |
| Cash reserve / investable capital constraint | `risk/policy.py::RiskPolicy.evaluate` (`investable_try`), `cli.py::run_cycle` (`cash_try_free - try_cash_target`) | Spending below configured TRY reserve | Reserve logic is enforced at intent-risk layer; not a separate wallet segregation mechanism. |
| Stage 4 action risk filter (`max_open_orders`, `max_position_notional`, daily loss, drawdown, min-profit including fees/slippage) | `services/risk_policy.py::RiskPolicy.filter_actions` | Breaching position and loss constraints; selling below fee+slippage+profit threshold | Stage 4-specific path; separate from Stage 3 risk engine. |
| Stage 4/6 risk mode switching (NORMAL/REDUCE/OBSERVE) | `domain/risk_budget.py::decide_mode`, `services/risk_budget_service.py::compute_decision` | Continued full-risk trading under drawdown/exposure/fee-budget breaches | Budget view uses accounting snapshot abstractions; behavior depends on input quality. |
| Stage 7 guardrails (drawdown, daily loss, consecutive losses, stale data, liquidity spread/volume) | `services/stage7_risk_budget_service.py::decide` | Running strategy in degraded market or after loss streaks | Stage 7 is dry-run gated elsewhere; production live effect unclear from Stage 7 runner path alone. |
| Kill-switch and safe-mode block submit/cancel writes | `services/execution_service.py::execute_intents`, `cancel_stale_orders` | Any exchange order side effects during emergency states | Blocking is code-level; if alternate execution path exists outside inspected files, unknown. |
| Idempotency key reservation/finalization for submit/cancel | `services/execution_service.py::execute_intents`/`cancel_stale_orders`, `services/state_store.py` idempotency schema/APIs | Duplicate order submissions and duplicate cancels under retries/restarts | Depends on consistent idempotency key derivation and DB integrity. |
| Uncertain submit/cancel reconciliation against exchange truth | `services/execution_service.py::_reconcile_submit`, `_reconcile_cancel`, `_match_existing_order` | Ghost orders / ambiguous order state after network/exchange errors | Reconciliation window is bounded; very delayed exchange visibility may still leave UNKNOWN states. |
| UNKNOWN-order probe backoff/escalation | `services/execution_service.py::_mark_unknown_unresolved` + unknown probe controls | Infinite silent unknown-order drift | Escalation can force safe-mode/kill-switch only when corresponding flags enabled. |
| API retry/backoff with 429 handling | `adapters/btcturk_http.py` `_get`, `_private_get`; `adapters/btcturk/rest_client.py::request`; retry helpers in `services/retry.py`, `adapters/btcturk/retry.py` | Immediate failure from transient network/5xx/429 | Retries are bounded; prolonged outage still fails cycles. |
| Client-side rate limiting | `adapters/btcturk/rate_limit.py::AsyncTokenBucket`, config knobs in `config.py` | Bursting above configured request rate | Applies where async rest client is used; Stage 3 sync client path has separate retry logic. |
| Secret scope/age controls | `security/secrets.py::validate_secret_controls`, `_load_settings` in `cli.py` | Over-privileged API scopes (withdraw), stale secrets, missing required scopes | Validation relies on `BTCTURK_SECRET_ROTATED_AT` being set accurately. |
| Request/secret sanitization for errors | `adapters/btcturk_http.py::_sanitize_request_*`, `security/redaction.py` | Leaking API keys/signatures/secrets in logs/errors | Sanitization exists for selected fields; full coverage for all custom log payloads is unknown. |

---

## B) Capital-flow explanation (“self-financing” mechanism)

### Observed capital allocation and sizing rules
- Stage 3 computes `cash_try_free` from balances, then `investable_try = max(0, cash_try_free - try_cash_target)` in cycle runtime; this value is fed into risk filtering. (`cli.py::run_cycle`, `risk/policy.py::RiskPolicy.evaluate`)
- Stage 3 risk policy applies:
  - max orders per cycle,
  - max open orders per symbol,
  - cooldown per symbol/side,
  - max notional per order,
  - total notional cap per cycle,
  - investable/cash-reserve cap.
- Strategy-level sizing in `ProfitAwareStrategyV1` is conservative and deterministic: sell 25% position on take-profit condition, buy up to min(TRY balance, 100 TRY) when flat and spread constraint is met.
- Stage 4/strategy-core allocation service applies fee-buffer and cash-target constrained deploy budget; buy/sell notionals are clipped by cash target, max position cap, per-intent cap, cycle cap, and min notional.

### Self-financing and compounding/reinvestment rules found
- `RiskBudgetPolicy.apply_self_financing(...)` explicitly splits **positive realized PnL** into:
  - compound-to-trading-capital (`profit_compound_ratio`, default 0.60),
  - reserve-to-treasury (`profit_treasury_ratio`, default 0.40).
- For **negative realized PnL delta**, loss is deducted from trading capital only; treasury is unchanged.
- Accounting ledger supports event types for `REBALANCE`, `TRANSFER`, and `WITHDRAWAL`; treasury and TRY balances are adjusted accordingly during deterministic replay.

### Fees and slippage assumptions
- Stage 4 risk policy uses `fee_bps_taker + slippage_bps_buffer + min_profit_bps` as minimum required edge for sell actions.
- Config has fee/slippage knobs (`FEE_BPS_MAKER`, `FEE_BPS_TAKER`, `SLIPPAGE_BPS_BUFFER`; Stage7-specific slippage/fees knobs also present).
- Ledger and metrics compute/report fees and slippage fields; anomaly detector includes fee and PnL divergence checks.

### Withdrawals / reserve rules
- Withdrawals are represented in accounting event model (`AccountingEventType.WITHDRAWAL`) and subtract from TRY balances in ledger replay.
- No autonomous withdrawal trigger policy was identified in inspected runtime orchestration files; withdrawal appears as event/accounting capability rather than an automated rule.

### Leverage / stop-loss / take-profit findings
- No explicit leverage or margin-borrowing controls were found in inspected source via keyword/code path search.
- Take-profit behavior exists in strategy (`reason="take_profit"`) with threshold based on `min_profit_bps` and position avg cost.
- Explicit stop-loss order placement logic was not found in Stage 3 strategy/execution files inspected; risk modes and drawdown limits are implemented as higher-level action gating.

---

## C) Top 10 failure modes (impact + where to mitigate)

1. **Unarmed-live misconfiguration bypass attempt**
   - Impact: unintended live order writes.
   - Existing mitigation location: `services/trading_policy.py`, `cli.py::_compute_live_policy`, `run_cycle` gate.

2. **Duplicate order submission under uncertainty/retries**
   - Impact: over-execution and unintended exposure.
   - Existing mitigation location: idempotency reserve/finalize in `execution_service.py`, `state_store.py` idempotency table.

3. **Ghost/unknown orders after submit/cancel errors**
   - Impact: stale local state, repeated risk miscalculation.
   - Existing mitigation location: `_reconcile_submit`, `_reconcile_cancel`, unknown probe escalation in `execution_service.py`.

4. **Drawdown breach without immediate risk-downshift**
   - Impact: rapid capital loss acceleration.
   - Existing mitigation location: `domain/risk_budget.py::decide_mode`, Stage4 risk policy (`max_daily_loss`, `max_drawdown`), Stage7 risk budget service.

5. **Cash reserve erosion from sizing path mismatch**
   - Impact: insufficient cash buffer for operations.
   - Existing mitigation location: `cli.py` investable computation + `risk/policy.py` investable constraint; allocation service fee-buffer/cash-target constraints.

6. **API rate-limit storm (429) causing degraded execution quality**
   - Impact: delayed/failed reconciliation and order actions.
   - Existing mitigation location: retry/backoff and 429 counters in `btcturk_http.py`, async rest client retry in `adapters/btcturk/rest_client.py`, token bucket in `adapters/btcturk/rate_limit.py`.

7. **Sensitive data leakage in error paths**
   - Impact: credential/signature exposure.
   - Existing mitigation location: `security/redaction.py` and request sanitizers in `btcturk_http.py`; secret control validation in `security/secrets.py`.

8. **Clock skew/nonce drift affecting private auth requests**
   - Impact: auth failures, repeated retries, missed windows.
   - Existing mitigation location: monotonic nonce in `btcturk_auth.py`; async clock sync service in `adapters/btcturk/clock_sync.py` (adapter wiring context-dependent).

9. **Single-instance contention (multiple bots using same DB/account)**
   - Impact: conflicting actions/orders and state races.
   - Existing mitigation location: `process_lock.py` used in `stage4-run` and `stage7-run`; Stage 3 `run` path lock use not evidenced.

10. **Dependency CVE exposure despite pinning**
   - Impact: exploitability in network/client/security libraries.
   - Existing mitigation location: pinned versions in `pyproject.toml` and `constraints.txt`; CI includes Bandit static security lint.
   - Gap: vulnerability scan results are not present in inspected files.

---

## Security-specific findings summary

- **Secrets handling**: environment/dotenv chain provider, runtime injection, API scope and rotation-age validation, and startup enforcement in `_load_settings`.
- **Signing/auth**: BTCTurk private auth uses HMAC-SHA256 signature over `api_key+timestamp`; monotonic millisecond nonce generator protects against duplicate/non-monotonic stamps.
- **Sensitive logging controls**: redaction patterns and request header/body/param sanitizers remove high-risk keys (`X-PCK`, signature, auth headers, secrets).
- **Dependency hygiene**: runtime and dev dependencies are pinned exact in project metadata and constraints; CI runs lint/type/tests/security lint (Bandit).

---

## Explicit unknowns (checked, but not evidenced)

- No explicit automated withdrawal policy/rules were found in runtime orchestrators; only accounting event support for withdrawals was identified.
- No leverage/margin borrowing controls were found by code search; repository appears spot-oriented in inspected modules.
- Full production wiring of async BTCTurk REST/WS clients into Stage 3 `run` path is not explicit in the inspected orchestration path (which uses sync exchange client construction).
- Vulnerability status of pinned dependencies (known CVEs) cannot be determined from repository files alone without external advisory scanning.
