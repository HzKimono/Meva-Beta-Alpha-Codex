# Meva-Beta-Stage End-to-End Audit Report

## 1) Executive Summary
- **Overall readiness rating:** **CONDITIONAL PASS**
- **Stage readiness ratings:**
  - **Stage 3:** **CONDITIONAL PASS**
  - **Stage 4:** **FAIL**
  - **Stage 5:** **FAIL**

### Top 10 risks (ranked)
1. **High** — Private side-effect requests (`POST/DELETE`) are not retried at transport layer; uncertain outcomes rely on reconciliation, which can still return unknown and leave operator ambiguity.  
2. **High** — `canonical_symbol()` removes underscores globally, while BTCTurk payload/params often use `pairSymbol` underscore form; this may cause symbol mismatches for some endpoints/venues depending on tolerance.  
3. **High** — No explicit file locking / multi-process safety in SQLite store; concurrent bot runs can race and violate operator assumptions.  
4. **Medium** — Risk policy enforces order-count/notional/cooldown, but does not enforce `MAX_POSITION_TRY_PER_SYMBOL` or portfolio allocation limits from settings.  
5. **Medium** — Strategy profit-taking trigger uses `bid >= avg_cost * (1 + min_profit_bps)` but does not include explicit fee/slippage/spread-adjusted realized profitability model.  
6. **Medium** — No Stage 5 universe discovery/auto-symbol selection module exists; symbol universe is static env config.  
7. **Medium** — Exchange rules provider silently falls back to conservative defaults on failures; this protects continuity but can cause over/under-filtering without hard alerting.  
8. **Medium** — Fills ingestion is adapter-optional (`get_recent_fills` default empty), so accounting can appear healthy while receiving no exchange fills if adapter lacks implementation.  
9. **Low** — `LIVE_TRADING_ACK` gate checks exact literal; robust but operationally brittle if deployment templating trims/normalizes unexpectedly.  
10. **Low** — Existing `AUDIT_REPORT.md` previously documented Stage 1; risk of stale docs if freeze artifacts include outdated report versions.

### What is safe to freeze now vs must change first
- **Safe to freeze now (Stage 3 baseline):** triple-gate safety semantics, idempotent action recording, deterministic tests, CI quality gates, dry-run workflow, structured logging.
- **Must change first before live scaling/Stage 4+ freeze:** position cap enforcement, explicit concurrent-run guard, stronger submit/cancel uncertain-outcome handling SOP, and Stage 4/5 feature completion (profit loop completeness + universe discovery).

## 2) Repository Inventory (100% coverage proof)
### Reviewed files (all tracked files)
- `.env.example`
- `.gitattributes`
- `.github/workflows/ci.yml`
- `AUDIT_REPORT.md`
- `README.md`
- `pyproject.toml`
- `scripts/guard_multiline.py`
- `src/btcbot/__init__.py`
- `src/btcbot/__main__.py`
- `src/btcbot/accounting/__init__.py`
- `src/btcbot/accounting/accounting_service.py`
- `src/btcbot/adapters/btcturk_auth.py`
- `src/btcbot/adapters/btcturk_http.py`
- `src/btcbot/adapters/exchange.py`
- `src/btcbot/cli.py`
- `src/btcbot/config.py`
- `src/btcbot/domain/accounting.py`
- `src/btcbot/domain/intent.py`
- `src/btcbot/domain/models.py`
- `src/btcbot/domain/symbols.py`
- `src/btcbot/logging_utils.py`
- `src/btcbot/risk/__init__.py`
- `src/btcbot/risk/exchange_rules.py`
- `src/btcbot/risk/policy.py`
- `src/btcbot/services/execution_service.py`
- `src/btcbot/services/market_data_service.py`
- `src/btcbot/services/portfolio_service.py`
- `src/btcbot/services/risk_service.py`
- `src/btcbot/services/state_store.py`
- `src/btcbot/services/strategy_service.py`
- `src/btcbot/services/sweep_service.py`
- `src/btcbot/services/trading_policy.py`
- `src/btcbot/strategies/__init__.py`
- `src/btcbot/strategies/base.py`
- `src/btcbot/strategies/context.py`
- `src/btcbot/strategies/profit_v1.py`
- `tests/test_accounting_stage3.py`
- `tests/test_btcturk_auth.py`
- `tests/test_btcturk_exchangeinfo_parsing.py`
- `tests/test_btcturk_http.py`
- `tests/test_btcturk_submit_cancel.py`
- `tests/test_cli.py`
- `tests/test_config.py`
- `tests/test_domain_models.py`
- `tests/test_env_example.py`
- `tests/test_exchangeinfo.py`
- `tests/test_execution_reconcile.py`
- `tests/test_execution_service.py`
- `tests/test_execution_service_live_arming.py`
- `tests/test_guard_multiline.py`
- `tests/test_logging_utils.py`
- `tests/test_risk_policy_stage3.py`
- `tests/test_state_store.py`
- `tests/test_state_store_stage3.py`
- `tests/test_strategy_stage3.py`
- `tests/test_sweep_service.py`
- `tests/test_trading_policy.py`

### NOT REVIEWED
- **None**.

### Generated/artifact files vs source
- Source/config/docs/tests/scripts: all files listed above.
- Runtime/generated (not tracked): SQLite state DB (`STATE_DB_PATH`, default `btcbot_state.db`) and Python bytecode caches from local runs.

## 3) Architecture Map (Expert-level)
- **cli**: startup, argument parsing, policy gating, service wiring, cycle orchestration.
- **adapters**: abstract exchange interface + BTCTurk HTTP/public-private implementation + auth signing.
- **services**:
  - market data (orderbook + symbol rules cache)
  - portfolio (balances)
  - strategy generation
  - risk filtering
  - execution + reconcile
  - legacy sweep planner
  - sqlite state persistence
- **domain**: intents, orders, symbols, accounting models.
- **risk/policy**: quantization + min_notional + caps/cooldown/open-order checks.
- **accounting/positions**: fill application, avg-cost, realized/unrealized PnL.
- **persistence**: `StateStore` tables for actions/orders/fills/positions/intents/meta.
- **logging/observability**: JSON formatter + structured extra fields + request IDs.

Dependency direction is mostly clean (`cli -> services -> adapters/domain`). No import cycles observed in source tree. The main coupling concern is `StrategyService` + `RiskService` dependence on broad `StateStore` API by `getattr`, which weakens explicit contracts.

### One cycle (text sequence)
`CLI.run_cycle -> policy validation -> build exchange -> StateStore init -> Execution.cancel_stale_orders -> Portfolio.get_balances -> MarketData.get_best_bids -> Accounting.refresh(fills+positions) -> Strategy.generate intents -> Risk.filter intents -> Execution.execute_intents (record_action + optional place/cancel + reconcile) -> StateStore.set_last_cycle_id -> close exchange`.

## 4) Safety & Live-Trading Controls (High Stakes)
### Side-effect capable methods
- `BtcturkHttpClient.submit_limit_order`, `place_limit_order`, `cancel_order` (private endpoints).
- `ExecutionService.execute_intents` (place path).
- `ExecutionService.cancel_stale_orders` (cancel path).

### Guard tracing
- Triple gate check in policy: kill switch, dry-run, live armed.
- CLI run gate blocks non-dry runs when not armed.
- Execution service rechecks before each live side effect.
- Kill switch short-circuits both cancel and place paths into logging-only mode.

### Bypass path assessment
- **Observed:** no direct live side-effect call in CLI path bypasses `ExecutionService` gating when normal run orchestration is used.
- **Residual risk:** direct adapter invocation by external code/tests is possible (outside CLI policy path), as expected in library-style architecture.

### Idempotency strategy
- Action dedupe via `actions.dedupe_key` unique index over `(action_type,payload_hash,time_bucket)`.
- Stable idempotency keys on intents; deterministic client order IDs derived from intent fields.
- Reconcile flow attempts `openOrders` and `allOrders` matching by client ID and fallback fields.

### Attack paths + mitigations
- Misconfigured env enabling live while assuming dry mode -> mitigated by triple gate + ACK literal.
- Network timeout after submit causing duplicate operator/manual resend -> partially mitigated by client order ID + reconcile.
- Concurrent bot processes -> not mitigated enough; add process-level lock or DB lock discipline.

## 5) External I/O Audit (HTTP, time, filesystem)
### HTTP endpoints used
- Public: `/api/v2/server/exchangeinfo`, `/api/v2/orderbook`.
- Private: `/api/v1/users/balances`, `/api/v1/openOrders`, `/api/v1/allOrders`, `/api/v1/order/{id}`, `/api/v1/order` (POST/DELETE).

### Retry/backoff
- Public GET `_get()` uses retry for timeout/429/5xx and transport, capped by attempt and total wait, parsing `Retry-After` (seconds/date) with per-sleep cap.
- Private `_private_request()` has **no retry loop** (safer for side effects).

### Timeouts
- `httpx.Timeout(timeout=..., connect=5s, read=10s, write=10s, pool=5s)` unless user passes custom timeout object.

### Logging sanitization
- Request params/json sanitize sensitive keys before attaching to `ExchangeError` context.
- API keys/secrets are not logged directly by observed code paths.

### Filesystem I/O
- `.env` via pydantic settings.
- SQLite DB created/updated at `STATE_DB_PATH`.
- No uncontrolled file writes beyond DB and normal logging stdout.

## 6) Data Integrity & Persistence Audit
### State DB schema
- Tables: `actions`, `orders`, `fills`, `positions`, `intents`, `meta`.
- Key invariants: primary keys on `order_id`, `fill_id`, `symbol`(positions), `intent_id`, `meta.key`; unique index on action dedupe key and intent idempotency key.

### Idempotency/reconcile
- Action-level dedupe window buckets prevent immediate duplicates.
- Fill inserts are `INSERT OR IGNORE` on `fill_id`.
- Execution reconciliation uses multi-source matching strategy after uncertain errors.

### Failure modes
- Missing DB: auto-created via `_init_db`.
- Corrupt DB: not explicitly handled; runtime sqlite exceptions propagate.
- Partial writes: context manager commits/rolls back transactionally per operation block.
- Concurrent runs: sqlite default settings without explicit busy timeout/locking policy; race risk remains.

### Backup/restore (freeze)
- Snapshot source + `.env.example` + CI + exact dependency metadata.
- Include DB migration/restore note: current schema evolves via additive `ALTER TABLE`; no migration version table.

## 7) Strategy, Risk, and Profit Loop Readiness (Stage 4/5 oriented)
### Current strategy behavior
- `ProfitAwareStrategyV1`:
  - If position exists and bid meets `avg_cost*(1+min_profit_bps)`, emits partial sell (25%).
  - Else if no position and spread <=1%, places conservative buy using min(TRY balance, 100 TRY).

### Profit-aware sell completeness
- Exists, but not explicitly fee-aware beyond avg_cost itself; no explicit slippage/bid-depth/fees buffer in sell trigger.

### Positions/PnL ledger
- Position ledger exists; handles buy/sell fill application and realized/unrealized PnL updates.
- Partial fill handling exists through additive fill processing.
- Accuracy depends on reliable fill ingestion; default adapter method may return no fills.

### Stage 5 discovery/allocation
- No universe discovery module detected; symbols come from static settings env list.
- No optimizer/allocation engine for dynamic portfolio weighting.

### Gap analysis
- **Exists now:** Stage 3 safety, Stage 4-leaning accounting/reconcile hooks, basic profit-take strategy.
- **Missing:** robust fill ingestion guarantees, explicit profit net-of-fee model, dynamic symbol discovery, allocation/risk budget framework.
- **Risky to deploy early:** autonomous symbol expansion and aggressive sell loops without full fee/slippage accounting.

## 8) Test Suite Audit (strict)
- Tests are deterministic/offline overall: extensive `httpx.MockTransport`, monkeypatching, in-memory/dry-run doubles.
- No direct real-network test dependency observed in pytest suite.
- Strong coverage for: safety gating, retry parsing/backoff behavior, exchangeinfo parsing, execution arming/reconcile, state store behavior.
- Coverage gaps remain in: multi-process concurrency, long-run DB corruption recovery, comprehensive Stage 5 features (absent by design).

### Minimal “golden” acceptance per stage
- **Stage 3:** config + health + dry-run cycle + arming guard tests + action dedupe/state tests.
- **Stage 4:** deterministic fill ingestion simulation, partial-fill realized/unrealized PnL assertions, submit/cancel uncertain reconcile scenarios.
- **Stage 5:** universe discovery scoring deterministic fixture tests, allocation constraints, symbol admission/removal hysteresis tests.

## 9) Code Quality & Maintainability
- Typing is generally strong (pydantic models/dataclasses/protocols), but dynamic `getattr` usage in services reduces interface explicitness.
- Error handling is mostly consistent with explicit exchange errors and structured logs.
- Logging is structured JSON with extra fields; correlation-like request IDs used in HTTP calls.
- CI alignment is good: guard script + ruff + compileall + pytest.
- Refactor candidates:
  1) Introduce explicit Protocols for `StateStore`-consuming services.
  2) Add migration versioning table.
  3) Centralize symbol normalization semantics for BTCTurk endpoint formatting.

## 10) Security Review (practical)
- Secret management via env and `SecretStr` settings fields; `.env.example` provides non-secret template.
- BTCTurk auth signing uses HMAC-SHA256 with base64-decoded secret; invalid base64 rejected.
- Dependencies are modern but not lockfile-pinned in repo; reproducibility depends on index state.
- GitHub Actions workflow is straightforward; no dangerous elevated permissions block observed.

## 11) Findings Table (Actionable)
| ID | Stage | Severity | Area | File(s) + lines | Symptom | Root cause | Recommended fix | Regression tests | Freeze blocker? |
|---|---|---|---|---|---|---|---|---|---|
| F-001 | 4/Cross | High | Execution reliability | `execution_service.py`, `btcturk_http.py` | Uncertain submit/cancel outcomes can remain unresolved | Side-effects non-retried + reconcile can return unknown | Add explicit unresolved-action escalation state + operator alert channel + replay-safe recovery command | Add tests for repeated unknown reconcile and recovery workflow | Yes |
| F-002 | Cross | High | Symbol normalization | `domain/symbols.py`, `btcturk_http.py` | Canonical symbols strip underscore; endpoint params may mismatch venue expectations | Global canonicalization reused for all contexts | Add explicit pair formatting helper per endpoint and preserve exchange-specific form | Add tests for BTC_TRY/BTCTRY roundtrip across all adapter calls | Yes |
| F-003 | 3/Cross | High | Persistence concurrency | `state_store.py` | Concurrent bot runs can race | No process lock / busy timeout strategy | Add single-instance lock file or sqlite BEGIN IMMEDIATE + busy_timeout and startup guard | Add integration test simulating two processes | Yes |
| F-004 | 4 | Medium | Risk policy completeness | `config.py`, `risk_service.py`, `risk/policy.py` | `MAX_POSITION_TRY_PER_SYMBOL` exists in config but not enforced | Setting not wired into policy evaluation | Implement position-size cap checks using mark prices and positions | Add policy tests for cap blocks/permits | Yes |
| F-005 | 4 | Medium | Profit realism | `strategies/profit_v1.py`, `accounting_service.py` | Profit sells may trigger without explicit fee/slippage cushion | Trigger checks min_profit_bps on avg_cost only | Add net-profit threshold formula including fee estimate + spread safety margin | Add strategy tests with fee/slippage fixtures | No |
| F-006 | 5 | Medium | Universe discovery | `config.py`, strategy/services modules | Static symbol list only | No discovery/scoring pipeline exists | Add Stage 5 module for discovery/scoring with conservative allowlist & risk caps | Add deterministic fixture tests | Yes |
| F-007 | 4 | Medium | Fill ingestion robustness | `adapters/exchange.py`, `accounting_service.py` | Accounting can process zero fills silently | `get_recent_fills` optional default empty | Make fill source explicit in live mode; alert on prolonged no-fill state with open orders | Add test for no-fill alert behavior | No |
| F-008 | Cross | Medium | Observability | `state_store.py`, `execution_service.py` | No explicit escalation artifact for unresolved actions | Metadata captures status but not alert lifecycle | Add unresolved_actions view/table + metrics/log counters | Add tests for unresolved lifecycle transitions | No |
| F-009 | Cross | Low | Reproducibility | `pyproject.toml` | No lockfile in repo | Floating transitive versions possible | Add pinned lock workflow (uv/pip-tools/poetry) | CI test using lock install path | No |
| F-010 | Cross | Low | Documentation drift | `AUDIT_REPORT.md`, `README.md` | Prior report stage mismatch risk | historical doc retained | Keep latest audit timestamped and archive old audits separately | Doc consistency check test/script | No |

## 12) Stage Freeze Checklist (what to run next)
### Windows PowerShell verification commands
```powershell
# clean venv
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip

# install deps
pip install -e ".[dev]"

# env template
Copy-Item .env.example .env -Force

# static + tests
ruff check .
python -m compileall src tests
pytest -q

# health + dry run
python -m btcbot.cli health
python -m btcbot.cli run --dry-run
```

Expected dry-run logs: kill-switch/dry-run policy messages and cycle completion summary with no live side effects.

### Freeze artifacts to archive
- Full source tree
- `pyproject.toml` and dependency export/lock artifact (if created)
- `.github/workflows/ci.yml`
- `README.md`, `.env.example`, this audit report
- Suggested tag: `v0.3.0-stage3-freeze-candidate`

### Release notes template
- Scope: Stage 3 baseline freeze candidate
- Safety: triple-gate enforcement summary
- Data: state schema and migration notes
- Tests: CI command matrix and results
- Known gaps: Stage 4/5 backlog + blockers

### Freeze recommendation
- **Recommendation: fix blockers first, then freeze.**
