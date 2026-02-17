# Verification Test Strategy (Safety-First, No-Funds-First)

Objective: validate trading-bot correctness and resilience with maximal offline coverage first, then minimal guarded live exchange validation.

## A) Test matrix (component × test type × tooling)

| Component | Offline test type | Existing tooling/tests in repo | Live exchange test need | Guardrail for live |
|---|---|---|---|---|
| Strategy logic | Unit tests with deterministic contexts (positions/orderbooks/balances) | `tests/test_strategy_stage3.py`; strategy context/models in `strategies/` | Not required for core logic correctness | N/A |
| Allocation / sizing / cash reserve | Unit + scenario tests for notional caps, min notional, cash target, max position caps | `tests/test_allocation_service.py`, `tests/test_portfolio_policy_service.py`, `tests/test_risk_policy_stage3.py` | Optional smoke validation only | Keep `DRY_RUN=true` until all scenario suites pass |
| Stage3 risk gating | Unit tests of policy blocks (cooldown/open-orders/notional/investable) | `tests/test_risk_policy_stage3.py`, `tests/test_trading_policy.py`, `tests/test_execution_service_live_arming.py` | Not required initially | N/A |
| Execution idempotency & reconcile | Unit/integration with mocked exchange responses (submit/cancel uncertain outcomes) | `tests/test_execution_service.py`, `tests/test_execution_reconcile.py`, `tests/test_btcturk_submit_cancel.py`, `tests/test_oms_idempotency.py` | Required for final confidence on exchange-specific edge semantics | Limit to single-symbol/single-order flow, low notional, immediate cancel window |
| Replay/backtest determinism | Deterministic replay and parity over CSV datasets | `tests/test_backtest_replay_determinism.py`, `tests/test_stage7_backtest_contracts.py`, `tests/test_replay_tools.py`, CLI `stage7-backtest`/`stage7-parity` | Not required | N/A |
| WS ingest + deterministic message handling | WS envelope/dispatch/backpressure tests with fake sockets and recorded fixtures | `tests/test_btcturk_ws_client.py`; fixture `tests/fixtures/btcturk_ws/channel_423_trade_match.json`; soak `tests/soak/test_market_data_soak.py` | Required only to validate production connectivity/reconnect behavior | Run live WS checks in observe-only mode (`KILL_SWITCH=true`) first |
| REST reliability / retry / rate-limit | Mocked 429/5xx with Retry-After and throttling tests | `tests/test_btcturk_rest_client.py`, `tests/test_btcturk_retry_reliability.py`, `tests/test_btcturk_rate_limit.py`, chaos `tests/chaos/test_resilience_scenarios.py` | Required to calibrate real exchange limits | Start with low request cadence; monitor `rest_429_rate` and retry metrics |
| Auth/signing/time-sync | Deterministic signature and nonce tests; clock-sync tests | `tests/test_btcturk_auth.py`, `tests/test_btcturk_clock_sync.py` | Required once with real creds/endpoints | Use read-only checks first (`health`, public endpoints), then private minimal call |
| Security controls (secrets/redaction) | Unit tests for secret control validation and logging hygiene | `tests/test_security_controls.py`, `security/secrets.py`, `security/redaction.py` | Not required first | Never persist secrets in fixture/output files |
| End-to-end cycle orchestration | Integration tests for stage runners and cycle persistence | `tests/test_stage4_cycle_runner.py`, `tests/test_stage7_run_integration.py`, `tests/test_state_store*.py` | Required final small canary | Single-cycle then short bounded loop with hard caps |

### Baseline verification gates (offline)
1. Static + unit/integration CI-equivalent pass (`make check`, unit tests excluding soak/chaos first).
2. Replay determinism and parity checks pass for fixed seed/time window.
3. Reliability suites pass (retry/rate-limit/ws-client/reconcile/idempotency).
4. Only after 1–3 pass: proceed to guarded live protocol.

---

## B) Minimal “safe live” test protocol (small notional, strict guardrails)

> This protocol is intentionally conservative and aligns with existing arming semantics and runbook guidance.

## Stage 0 — Pre-live (no side effects)
- Keep safety defaults enabled: `DRY_RUN=true`, `KILL_SWITCH=true`, `LIVE_TRADING=false`.
- Run `health`, `doctor`, and one dry-run cycle (`run --dry-run --once` or `stage4-run --dry-run --once`).
- Verify no invariant failures and no stale-data/retry storms in logs/metrics.

## Stage 1 — Read-only exchange validation
- Keep `KILL_SWITCH=true` (or `SAFE_MODE=true`) to block submit/cancel writes.
- Validate:
  - auth/signing path by private read endpoints only (balances/open orders/fills read path),
  - clock sync sanity (no skew alarms),
  - rate-limit behavior (no sustained 429).
- Exit criteria:
  - stable reads over at least several cycles,
  - no uncontrolled retries/reconnect storms.

## Stage 2 — Single-order minimal notional canary
- Arm live only after Stage 1 is stable:
  - `DRY_RUN=false`, `KILL_SWITCH=false`, `LIVE_TRADING=true`, `LIVE_TRADING_ACK=I_UNDERSTAND`.
- Apply strict caps from pilot profile:
  - low `NOTIONAL_CAP_TRY_PER_CYCLE`, low `MAX_NOTIONAL_PER_ORDER_TRY`, `MAX_ORDERS_PER_CYCLE=1..2`, `MAX_OPEN_ORDERS_PER_SYMBOL=1`, conservative cooldown.
- Run a **single-cycle** command (`stage4-run --once` preferred for lifecycle/accounting integration).
- Immediate post-check:
  - reconcile open orders vs exchange truth,
  - confirm idempotency records/action metadata persisted,
  - confirm no unknown-order escalation.

## Stage 3 — Bounded live loop canary
- Execute bounded loop (e.g., max 5–20 cycles), not infinite.
- Monitor alert signals from runbook/SLO docs:
  - `rest_429_rate`, `rest_retry_rate`, `ws_reconnect_rate`, `stale_market_data_rate`, `reconcile_lag_ms`, `order_submit_latency_ms`, `circuit_breaker_state`.
- Abort criteria (any one triggers immediate safe-mode/kill-switch):
  - repeated 429/retry storm,
  - reconcile lag spike or unknown-order escalation,
  - stale-market-data sustained.

## Stage 4 — Exit / rollback safety
- On anomaly: set `SAFE_MODE=true` (or `KILL_SWITCH=true`) and restart.
- Preserve DB/log artifacts for post-mortem; do not continue live until root cause is understood.

---

## C) Required fixtures and datasets

## 1) Recorded WS streams
- Existing: `tests/fixtures/btcturk_ws/channel_423_trade_match.json`.
- Add/maintain representative fixtures for:
  - normal trade cadence,
  - burst traffic (backpressure),
  - reconnect gap/rejoin sequence,
  - malformed envelope cases.
- Validation target: parser stability + queue behavior + reconnect counters.

## 2) Sample REST responses
- Existing exchange-info fixtures:
  - `tests/fixtures/btcturk_exchangeinfo_min_notional_present.json`
  - `tests/fixtures/btcturk_exchangeinfo_min_notional_absent.json`
- Capture utility exists: `scripts/capture_exchangeinfo_fixture.py` (public endpoint capture).
- Add/maintain mocked response sets for:
  - auth errors, 429 with Retry-After, 5xx transient failures,
  - order submit/cancel accepted/rejected/unknown reconciliation paths,
  - partial-fill/open-order snapshots.

## 3) Golden replay datasets
- Dataset contract already defined (`candles/`, `orderbook/`, optional `ticker/` CSV per symbol).
- Use `replay-init` synthetic generation for deterministic baseline; validate with `replay.validate` and parity fingerprints.
- Maintain a small set of golden windows (e.g., 1h, 24h) with expected fingerprints and stage7 table counts.

## 4) Golden live-canary artifacts (sanitized)
- For each safe-live run, retain:
  - cycle audit rows,
  - idempotency keys/actions/orders snapshots,
  - metrics export snapshots,
  - redacted logs.
- Purpose: reproducibility and rollback-grade incident analysis without exposing secrets.

---

## Safety guardrails summary (mandatory for any live test)
- Start from `DRY_RUN=true` + `KILL_SWITCH=true` + `SAFE_MODE=true` for first validation pass.
- Use live arming only with explicit ACK and only after dry-run stability.
- Keep notional/order caps conservative and symbol universe narrow.
- Prefer single-cycle commands before any loop.
- Define hard abort thresholds and enforce immediate safe-mode fallback.
