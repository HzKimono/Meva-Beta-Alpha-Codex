# Financial Correctness & Safety Audit (Code-Proven)

## Scope
This report proves safety and accounting behavior from source code only. Every claim includes file + line evidence. If missing, it is marked **GAP**.

---

## 1) Money/Quantity Type Inventory

| Concept | Runtime type(s) | Rounding/quantization | Precision & constraints | Conversion points |
|---|---|---|---|---|
| Stage3 balances (`Balance.free/locked`) | `float` | none in model | no intrinsic scale constraints in `Balance`; constraints enforced later by rules validation | adapter payload parsing and strategy context conversions to `Decimal` (`Decimal(str(...))`) (`src/btcbot/domain/models.py` L65-L69; `src/btcbot/strategies/profit_v1.py` L47-L54; `src/btcbot/services/strategy_service.py` L37-L48) |
| Stage3 orders (`Order.price/quantity`) | `float` | quantized to rules before live submit (`ROUND_DOWN`) | `validate_order` enforces price/qty positive, tick/step exactness, min/max, min_total | `ExecutionService` converts intent float->`Decimal` for quantize/validate, then float for exchange call (`src/btcbot/domain/models.py` L111-L121, L282-L323; `src/btcbot/services/execution_service.py` L321-L334) |
| Symbol rules | `Decimal` (`min_total`, `tick_size`, `step_size`) | `quantize_price`/`quantize_quantity` with `ROUND_DOWN` | exchange min/max price/qty and min notional validated | pair-info mapping + exchange-rules extraction (`src/btcbot/domain/models.py` L80-L90, L325-L337; `src/btcbot/services/exchange_rules_service.py` L267-L315, L506-L539) |
| Stage4 orders/fills/positions | `Decimal` | `Quantizer.quantize_price/qty` with `ROUND_DOWN` | `validate_min_notional`, oversell detection, mode-aware reject paths | Stage4 execution + accounting services (`src/btcbot/domain/stage4.py` L97-L121; `src/btcbot/services/execution_service_stage4.py` L121-L134; `src/btcbot/services/accounting_service_stage4.py` L128-L134) |
| Fees | `Decimal` | no arbitrary rounding; arithmetic in Decimal | Stage3/4: non-quote/non-TRY fees can be ignored or audit-noted; Stage7 books TRY fee events | stage3 accounting fee currency check, stage4 fee notes, stage7 fee event generation (`src/btcbot/accounting/accounting_service.py` L57-L69; `src/btcbot/services/accounting_service_stage4.py` L109-L114, L167-L172; `src/btcbot/services/stage7_cycle_runner.py` L479-L535) |
| PnL (realized/unrealized/gross/net), equity, drawdown | `Decimal` | no quantize in ledger math (exact Decimal ops) | oversell invariant in ledger and accounting; max drawdown computed as ratio | ledger apply + snapshot/report computation (`src/btcbot/domain/ledger.py` L81-L147, L150-L181; `src/btcbot/services/ledger_service.py` L196-L257, L258-L320) |
| Stage7 OMS price/qty/notional | `Decimal` | fill slicing quantized to `0.00000001` in simulator partial branch | rejected when qty/price/notional <=0; allowed transitions prevent invalid states | OMS process + order state transitions (`src/btcbot/services/oms_service.py` L58-L66, L81-L97, L155-L174, L465-L470) |
| Rate limits / retry delays | `float` seconds and `int` ms | deterministic exponential + jitter for retry | bounded by configured max attempts/delays | `retry_with_backoff` + token bucket limiter (`src/btcbot/services/retry.py` L19-L58; `src/btcbot/services/rate_limiter.py` L6-L46) |
| Leverage | **NOT FOUND** | n/a | no leverage model/constraint present | searched `src tests docs` for `leverage|margin` with no matches (**GAP if leverage trading planned**) |
| Funding/interest (futures) | **NOT FOUND** | n/a | no futures funding/interest postings in ledger | searched `src tests docs` for `funding|interest|futures|borrow` with no matches (**GAP only if futures scope intended**) |

Additional type notes:
- Config mixes `float` (e.g., `target_try`, `dry_run_try_balance`) and `Decimal` (risk/fees/notional controls), so conversion boundaries are material. (`src/btcbot/config.py` L31-L60)
- `parse_decimal` normalizes int/float/string into Decimal via `Decimal(str(...))`, reducing binary-float propagation where used. (`src/btcbot/domain/models.py` L18-L28)

---

## 2) All trade-enabling side effects + mandatory gates

## 2.1 Stage3/legacy execution side effects

1. **Submit order (live)**: `ExecutionService.execute_intents` -> `exchange.place_limit_order(...)`. (`src/btcbot/services/execution_service.py` L247-L356, L328-L335)
   - Mandatory gates before side effect:
     - CLI policy block when not dry-run and not armed. (`src/btcbot/cli.py` L479-L491)
     - per-action check `_ensure_live_side_effects_allowed()`. (`src/btcbot/services/execution_service.py` L279-L281, L628-L637)
     - kill switch short-circuits execution to no side effects. (`src/btcbot/services/execution_service.py` L262-L275)

2. **Cancel order (live)**: `ExecutionService.cancel_stale_orders` -> `exchange.cancel_order(...)`. (`src/btcbot/services/execution_service.py` L129-L245, L193-L195)
   - Mandatory gates:
     - kill switch blocks cancels and only logs. (`src/btcbot/services/execution_service.py` L136-L143)
     - live-side-effects check if not dry-run. (`src/btcbot/services/execution_service.py` L166-L168)

3. **Credentialed private API use**: `BtcturkHttpClient._private_request` (all private submit/cancel/fills). (`src/btcbot/adapters/btcturk_http.py` L241-L310)
   - Mandatory gates:
     - api key + secret required else `ConfigurationError`. (`src/btcbot/adapters/btcturk_http.py` L248-L252)
     - signing via `build_auth_headers` / HMAC signature. (`src/btcbot/adapters/btcturk_http.py` L255-L260; `src/btcbot/adapters/btcturk_auth.py` L8-L31)

## 2.2 Stage4 execution side effects

4. **Submit order (stage4 live path)**: `ExecutionService.execute_with_report` -> `exchange.submit_limit_order(...)`. (`src/btcbot/services/execution_service_stage4.py` L43-L57, L147-L153)
   - Mandatory gates:
     - kill switch immediate block. (`src/btcbot/services/execution_service_stage4.py` L44-L48)
     - `live_mode = is_live_trading_enabled and not dry_run`; if not live mode, records simulated submit only. (`src/btcbot/services/execution_service_stage4.py` L52-L56, L136-L145)
     - exchange rules must resolve + min-notional pass (else rejected). (`src/btcbot/services/execution_service_stage4.py` L90-L105, L121-L134)
     - submit dedupe check prevents duplicate side effects. (`src/btcbot/services/execution_service_stage4.py` L69-L88)

5. **Cancel order (stage4 live path)**: `ExecutionService.execute_with_report` -> `exchange.cancel_order_by_exchange_id(...)`. (`src/btcbot/services/execution_service_stage4.py` L178-L212)
   - Mandatory gates:
     - terminal-order short-circuit. (`src/btcbot/services/execution_service_stage4.py` L183-L184)
     - missing exchange id -> error record, no call. (`src/btcbot/services/execution_service_stage4.py` L186-L201)
     - non-live mode simulates cancel only. (`src/btcbot/services/execution_service_stage4.py` L203-L207)

## 2.3 Adapter implementations that actually perform write calls

6. **BTCTurk write endpoints**:
   - submit: `_private_request("POST", "/api/v1/order", ...)` in `submit_limit_order` and legacy place. (`src/btcbot/adapters/btcturk_http.py` L828-L848, L883-L902)
   - cancel: `_private_request("DELETE", "/api/v1/order", ...)` in `cancel_order`/`cancel_order_by_client_order_id`. (`src/btcbot/adapters/btcturk_http.py` L859-L862, L914-L917)
   - credentials + signing gate at `_private_request`. (`src/btcbot/adapters/btcturk_http.py` L248-L260)

7. **Dry-run adapters** (`DryRunExchangeClient*`) mutate in-memory order lists only (non-external). (`src/btcbot/adapters/btcturk_http.py` L1088-L1138)

---

## 3) Risk Controls Proof

| Control | Enforcement location | Phase | Notes / Bypass analysis |
|---|---|---|---|
| Kill switch global side-effect block | CLI policy block + execution service checks | pre-execution | Enforced in stage3 CLI block + stage3/stage4 execution services; direct adapter invocation outside services would bypass (**GAP: architectural, not runtime path**). (`src/btcbot/cli.py` L479-L491; `src/btcbot/services/execution_service.py` L136-L143; `src/btcbot/services/execution_service_stage4.py` L44-L48) |
| Live arming (`LIVE_TRADING_ACK`) | settings validation + runtime policy check | startup + pre-execution | `Settings` rejects invalid live combos; runtime policy still checks each run. (`src/btcbot/config.py` L487-L503, L535-L536; `src/btcbot/services/trading_policy.py` L12-L24) |
| Max orders per cycle | stage3 risk policy | pre-intent | slices intents list to cap. (`src/btcbot/risk/policy.py` L49-L50) |
| Max open orders per symbol | stage3 risk policy | pre-intent | blocks with explicit reason. (`src/btcbot/risk/policy.py` L51-L53) |
| Cooldown between intents | stage3 risk policy | pre-intent | compares last intent timestamps. (`src/btcbot/risk/policy.py` L55-L61) |
| Notional cap per cycle | stage3 risk policy | pre-intent | rejects if cumulative notional exceeds cap. (`src/btcbot/risk/policy.py` L71-L74) |
| Tick/step/min-notional rule checks | stage3 quantize/validate + stage4 rules/min-notional + stage7 rules boundary | pre-execution | stage7 can enforce metadata-required and degrade/skip behavior. (`src/btcbot/domain/models.py` L282-L323; `src/btcbot/services/execution_service_stage4.py` L90-L134; `src/btcbot/services/exchange_rules_service.py` L425-L460, L490-L539) |
| Max daily loss / max drawdown | stage4 `RiskPolicy.filter_actions`; stage7 risk budget decision | pre-execution + mode gating | stage4 can reject all actions; stage7 can force OBSERVE_ONLY. (`src/btcbot/services/risk_policy.py` L46-L57; `src/btcbot/services/stage7_risk_budget_service.py` L50-L68, L85-L93) |
| Max gross exposure / position concentration | stage4 risk budget (`decide_mode`) and signals computation | pre-execution | sets `REDUCE_RISK_ONLY` when exposure limits exceeded. (`src/btcbot/domain/risk_budget.py` L52-L67; `src/btcbot/services/risk_budget_service.py` L51-L67, L116-L140) |
| Fee budget cap | stage4 risk budget decide_mode | pre-execution | mode downgrade when fees exceed day cap. (`src/btcbot/domain/risk_budget.py` L64-L67) |
| Rate limiting | stage7 token-bucket in OMS | pre-execution | emits `THROTTLED` and skips intent processing. (`src/btcbot/services/oms_service.py` L127-L131, L200-L215) |
| Retry budget / giveup | stage7 OMS retry policy | pre-execution reliability | deterministic retries then `RETRY_GIVEUP` event. (`src/btcbot/services/oms_service.py` L264-L298; `src/btcbot/services/retry.py` L40-L58) |
| Post-trade reconciliation | stage3 uncertain submit/cancel reconciliation + stage4 reconcile service | post-execution | closes uncertainty windows; still eventual consistency window exists on exchange outages. (`src/btcbot/services/execution_service.py` L203-L228, L357-L360; `src/btcbot/services/stage4_cycle_runner.py` L91-L92, L248-L250) |

### Potential bypass paths
- **Direct adapter calls** (`BtcturkHttpClient.submit_limit_order/cancel_order`) bypass service-level policy checks if called outside orchestrated runners. This is not observed in normal runtime wiring but is an architectural bypass vector. (**GAP**) (`src/btcbot/adapters/btcturk_http.py` L828-L862, L914-L917)
- **Multi-instance runners on same DB**: controls are idempotent in many tables, but concurrent bot instances can still race at business level (e.g., both decide to submit different client IDs). (**GAP**) (`src/btcbot/services/state_store.py` L111-L127, L1201-L1224)

---

## 4) Ledger / Accounting Proof

## 4.1 Canonical source of truth

- Canonical event store is `ledger_events` table with `event_id` PK and unique `exchange_trade_id` index/fallback unique key. (`src/btcbot/services/state_store.py` L1295-L1335)
- Stage7 OMS canonical lifecycle state is `stage7_order_events` (append-only) + `stage7_orders` snapshot. (`src/btcbot/services/state_store.py` L517-L554)

## 4.2 PnL/equity/drawdown computation path

- `apply_events` produces lot-based realized PnL and fee accumulation by currency; oversell raises invariant violation. (`src/btcbot/domain/ledger.py` L81-L137, L122-L125)
- `compute_realized_pnl`, `compute_unrealized_pnl`, `compute_max_drawdown` are Decimal-based deterministic reducers. (`src/btcbot/domain/ledger.py` L150-L181)
- `LedgerService.financial_breakdown/snapshot` computes gross/net/equity/turnover and persists snapshot context. (`src/btcbot/services/ledger_service.py` L196-L257, L258-L320)

## 4.3 Edge-case handling validation

a) **Partial fills**
- Stage7 OMS explicitly emits `PARTIAL_FILL` then `FILLED` when slices >1. (`src/btcbot/services/oms_service.py` L341-L371)
- Stage3/4 ingest relies on exchange fills stream + dedupe by `fill_id`. (`src/btcbot/services/accounting_service_stage4.py` L37-L87, L98-L101)

b) **Fees in base/quote**
- Stage3 accounting ignores non-quote fees with warning (risk of understated fee burden). (`src/btcbot/accounting/accounting_service.py` L57-L69)
- Stage4 logs fee conversion missing for non-TRY fee asset and records audit note. (`src/btcbot/services/accounting_service_stage4.py` L109-L114, L167-L172)
- Stage7 currently books fees in TRY for simulated OMS fills. (`src/btcbot/services/stage7_cycle_runner.py` L479-L535)
- **GAP:** no implemented generic FX fee conversion pipeline for non-TRY fees in stage3/4 live accounting.

c) **Funding/interest (futures)**
- **NOT FOUND** (no leverage/futures/funding domain/events). command used: `rg -n "leverage|margin|futures|funding|interest|borrow" src tests docs`.
- **GAP only if futures are expected**.

d) **Cancellations and replace**
- Replace modeled as `replace_cancel` + `replace_submit` lifecycle action pair, not atomic exchange replace. (`src/btcbot/services/order_lifecycle_service.py` L55-L83)
- Cancel paths update order status and reconciliation metadata in stage3/stage4. (`src/btcbot/services/execution_service.py` L203-L244; `src/btcbot/services/execution_service_stage4.py` L178-L212)

e) **Clock/timezone drift**
- UTC normalization helper (`ensure_utc`) used in ledger sorting and state-store writes; stage7 anomaly config includes clock skew threshold. (`src/btcbot/domain/ledger.py` L77-L79, L184-L187; `src/btcbot/services/stage4_cycle_runner.py` L110-L118)
- **GAP:** no dedicated exchange-server time sync/offset correction component beyond anomaly detection.

## 4.4 Double-counting / missing-event scenarios

- Prevented duplicates:
  - fills/events: `INSERT OR IGNORE` on fill/event primary keys. (`src/btcbot/services/state_store.py` L1643-L1663, L2411-L2441)
  - applied fill marker (`applied_fills`) prevents replay re-application. (`src/btcbot/services/state_store.py` L2283-L2289)
- Potential misses:
  - if exchange delivers fill without stable ID and fallback composition changes, dedupe risk exists across schema variants. stage4 attempts deterministic fallback fill id. (`src/btcbot/services/accounting_service_stage4.py` L57-L64)

---

## 5) Idempotency & Consistency Proof

- Stage3 action idempotency window key: `action_type:payload_hash:time_bucket`; duplicate actions ignored. (`src/btcbot/services/state_store.py` L1511-L1535)
- Stage7 idempotency table (`stage7_idempotency_keys`) enforces one key/one payload; conflict raises `IdempotencyConflictError`. (`src/btcbot/services/state_store.py` L1201-L1224)
- Stage7 OMS event dedupe uses deterministic `event_id` + `INSERT OR IGNORE`; orders/events persisted in one transaction. (`src/btcbot/services/oms_service.py` L376-L379, L416-L439; `src/btcbot/services/state_store.py` L1160-L1178)
- Stage7 cycle persistence is atomic transaction (`save_stage7_cycle`), and stage-specific failure markers identify where commit failed. (`src/btcbot/services/state_store.py` L783-L972)
- Re-run duplicate ledger prevention by `event_id` and unique indexes on exchange trade ids. (`src/btcbot/services/state_store.py` L1298-L1335, L2411-L2441)

### Race-condition risks
- SQLite uses WAL + busy timeout + `BEGIN IMMEDIATE`, which protects DB integrity but not high-level multi-bot decision races. (**GAP**) (`src/btcbot/services/state_store.py` L94-L120)
- Retry loops may replay intent processing; idempotency keys/events mitigate, but only where keys are consistently used. (`src/btcbot/services/oms_service.py` L217-L252, L264-L298)

---

## 6) Security & Secrets Audit

- Secret loading:
  - `Settings` uses `SecretStr` for API key/secret loaded from env/.env. (`src/btcbot/config.py` L22-L24, L15-L20)
- Required live env constraints:
  - model validator requires ACK, kill-switch off, and key+secret when live trading true. (`src/btcbot/config.py` L495-L503)
- Signing point:
  - `build_auth_headers` computes HMAC signature from base64 secret and stamp. (`src/btcbot/adapters/btcturk_auth.py` L8-L31)
- Redaction/safe logging:
  - request params/json are sanitized to remove API/secret/signature fields before attached to exceptions. (`src/btcbot/adapters/btcturk_http.py` L136-L155, L291-L293, L307-L309)
- **Key-printing check:**
  - No direct key/secret logging found in adapter/runtime paths reviewed; errors log sanitized payloads and flags such as `request_has_json`. (`src/btcbot/adapters/btcturk_http.py` L280-L285, L291-L293)
- Config source and defaults:
  - `.env.example` keeps defaults safe (`DRY_RUN=true`, `KILL_SWITCH=true`, `LIVE_TRADING=false`). (`.env.example` L6-L11)

---

## Invariant Tests We Must Have (Release-critical)

| # | Invariant | Why it matters | Where it can break | Suggested test location | Acceptance criteria |
|---|---|---|---|---|---|
| 1 | Live submit blocked unless all gates armed | Prevent unintended real trades | CLI/service gate drift | `tests/test_cli.py` + execution service tests | unarmed live run returns code 2; no submit call |
| 2 | Kill switch blocks submit/cancel in stage3 | Emergency stop correctness | execution path regressions | `tests/test_execution_service.py` | with kill switch, submitted/canceled == 0 |
| 3 | Kill switch blocks stage4 writes | stage4 canary safety | `execution_service_stage4` changes | `tests/test_stage4_services.py` | execute report totals all zero in kill switch mode |
| 4 | Stage4 min-notional reject before submit | avoid invalid orders | rules parsing/fallback drift | `tests/test_stage4_services.py` | rejected increments; no exchange submit |
| 5 | Stage4 dedupe on repeated client_order_id | duplicate live order prevention | dedupe key logic regression | `tests/test_stage4_cycle_runner.py` | second cycle logs deduped and no second submit |
| 6 | Stage3 action dedupe window works | retry safety | `record_action` bucket logic | `tests/test_execution_service.py` | duplicate payload in same bucket returns None |
| 7 | Stage7 idempotency same payload ignored | deterministic reruns | OMS idempotency layer | `tests/test_oms_idempotency.py` | DUPLICATE_IGNORED emitted, no extra submit |
| 8 | Stage7 idempotency conflicting payload rejects | consistency/safety | key reuse bugs | `tests/test_oms_idempotency.py` | IDEMPOTENCY_CONFLICT + REJECTED emitted |
| 9 | OMS transition graph forbids illegal transitions | ledger/order correctness | state-machine drift | `tests/test_oms_state_machine.py` | illegal transition leaves state unchanged |
|10| OMS retry give-up emits events deterministically | operational auditability | retry changes | `tests/test_oms_retry_backoff.py` | RETRY_SCHEDULED count matches config, then RETRY_GIVEUP |
|11| Ledger oversell invariant throws | prevents negative inventory corruption | event ordering/data gaps | `tests/test_ledger_domain.py` | apply_events raises ValueError on oversell |
|12| Fee event invariant enforced (`side=None,qty=0,price=None`) | avoid fee/fill confusion | ingestion mapper bugs | `tests/test_ledger_domain.py` | invalid FEE event raises ValueError |
|13| Stage4 accounting oversell raises integrity error | protects realized PnL correctness | fill ingestion/order mismatch | `tests/test_stage4_services.py` | oversell -> `AccountingIntegrityError` |
|14| Non-TRY fee produces audit note | explicit known limitation tracking | silent under-reporting | `tests/test_stage4_services.py` | cycle audit contains `fee_conversion_missing:*` |
|15| Stage7 ledger event dedupe works on rerun | no double-counted PnL | rerun parity/idempotency | `tests/test_backtest_replay_determinism.py` | rerun does not increase duplicate ledger events |
|16| Drawdown mode transitions monotonic under cooldown | risk circuit-breaker stability | risk-budget regressions | `tests/test_stage7_risk_budget_service.py` | mode not less restrictive before cooldown expires |
|17| Rate limiter throttles excess intents | exchange safety and anti-burst | limiter parameter drift | `tests/test_oms_throttling.py` | THROTTLED events present when burst exceeded |
|18| Sanitized error payload excludes secrets/signature | secret hygiene | adapter error logging changes | `tests/test_btcturk_http.py` | `request_json/request_params` never include blocked keys |

Evidence basis for table scope: gate logic, risk, OMS, ledger invariants, and sanitization are in cited modules above. (`src/btcbot/services/trading_policy.py` L12-L24; `src/btcbot/services/oms_service.py` L200-L252; `src/btcbot/domain/ledger.py` L122-L137; `src/btcbot/adapters/btcturk_http.py` L136-L155)

---

## Safety Gate Checklist (Release Blocker)

1. **Global live gates pass/fail test** (KILL_SWITCH/DRY_RUN/LIVE_TRADING/ACK) in CI. (`src/btcbot/services/trading_policy.py` L12-L24; `src/btcbot/config.py` L487-L503)
2. **No side-effect path skips execution service** (architectural lint/review rule). (**GAP**) (`src/btcbot/adapters/btcturk_http.py` L828-L862)
3. **Stage4 min-notional + rules enforcement remains hard-fail pre-submit**. (`src/btcbot/services/execution_service_stage4.py` L90-L134)
4. **Stage3/Stage7 idempotency tests green** (action dedupe + stage7 key dedupe/conflict). (`src/btcbot/services/state_store.py` L1511-L1535, L1201-L1224)
5. **OMS retry/throttle tests green**. (`src/btcbot/services/oms_service.py` L200-L298)
6. **Ledger oversell + fee-event invariants green**. (`src/btcbot/domain/ledger.py` L122-L137)
7. **Non-TRY fee conversion limitation explicitly accepted or fixed**. (**GAP**) (`src/btcbot/services/accounting_service_stage4.py` L109-L114)
8. **Multi-instance policy defined** (single-writer lock or orchestration guarantee). (**GAP**) (`src/btcbot/services/state_store.py` L94-L120)
9. **Secret redaction tests green** for adapter errors. (`src/btcbot/adapters/btcturk_http.py` L136-L155, L291-L293)
10. **Default env remains safe** in templates/docs (`DRY_RUN=true`, `KILL_SWITCH=true`). (`.env.example` L6-L11)

---

## GAP Summary + Minimal Fix Approach (no code edits here)

- **GAP-1: direct adapter bypass risk**
  - Issue: service-level gates can be bypassed by direct adapter calls.
  - Minimal fix: add a single policy-enforcing façade/port for exchange writes and forbid direct adapter imports in runners via lint rule.
  - Verify locations: adapter write methods in `btcturk_http.py`. (`src/btcbot/adapters/btcturk_http.py` L828-L862, L914-L917)

- **GAP-2: non-TRY fee conversion incomplete (stage3/stage4)**
  - Issue: non-TRY fees ignored/audited, potentially understating cost.
  - Minimal fix: centralized FX conversion service with deterministic rate source + fallback behavior.
  - Verify locations: stage3/stage4 accounting fee handling. (`src/btcbot/accounting/accounting_service.py` L57-L69; `src/btcbot/services/accounting_service_stage4.py` L109-L114)

- **GAP-3: multi-instance race policy not explicit**
  - Issue: SQLite transaction safety ≠ business-level single-run safety.
  - Minimal fix: advisory lock table/OS file lock at startup and explicit operator docs for single active writer.
  - Verify locations: transaction implementation. (`src/btcbot/services/state_store.py` L111-L127)

- **GAP-4: no leverage/futures/funding model**
  - Issue: if futures are intended, risk/accounting coverage is incomplete.
  - Minimal fix: explicit spot-only assertion in docs/doctor, or add futures domain/events/risk caps before enabling futures.
  - Verification search command: `rg -n "leverage|margin|futures|funding|interest|borrow" src tests docs` (no matches).

