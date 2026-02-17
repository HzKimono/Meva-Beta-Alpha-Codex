# Guarded Live Runbook (BTCTurk, Stage 3)

This runbook is the operator playbook for progressing from **no-side-effects validation** to a **bounded live canary** for `btcbot run`.

> Scope: Stage 3 runtime (`btcbot run`) on BTCTurk.

---

## A) Scope and goals

## What “guarded live” means

Guarded live is a controlled rollout model:

1. Start in observe-only (`DRY_RUN=true` or `--dry-run`) and verify runtime health.
2. Validate exchange/network connectivity without writing orders.
3. Arm live trading only with explicit operator intent and hard caps.
4. Run a bounded canary (`--max-cycles`) with explicit stop/abort rules.

## What it does **not** mean

- Not unattended “set and forget” production.
- Not unlimited runtime with relaxed limits.
- Not bypassing arm gates or stale-data protections.

## Safety invariants (must remain true)

- **Fail-closed on stale market data:** if data exceeds freshness limits, cycle is blocked and decision envelope is emitted (`reason_code=market_data:stale`).
- **No live side effects unless fully armed:**
  - `LIVE_TRADING=true`
  - `LIVE_TRADING_ACK=I_UNDERSTAND`
  - `DRY_RUN=false`
  - `KILL_SWITCH=false`
  - `SAFE_MODE=false`
- **Safe mode wins:** `SAFE_MODE=true` forces observe-only behavior.
- **Single instance lock:** Stage 3 runtime acquires a lock per DB/account scope; parallel instances must not be run for the same scope.

---

## B) Prerequisites checklist

Before Stage 0, verify all items.

- [ ] **Environment file prepared** (example: `.env.pilot`) with no plaintext secrets committed.
- [ ] **Required secrets present at runtime** for eventual live stage:
  - `BTCTURK_API_KEY`
  - `BTCTURK_API_SECRET`
- [ ] **Redaction expectation:** never paste full secret values into logs/tickets/chat.
- [ ] **DB path set and writable:** `STATE_DB_PATH=...` (or pass `--db` to tools that support it).
- [ ] **Dataset optionality understood:** replay `--dataset` is optional unless running replay/backtest tools.
- [ ] **Required folders exist:** directory for `STATE_DB_PATH`, plus optional export/log directories.
- [ ] **Clock/time sync check completed:** host NTP healthy; drift is small and stable.
- [ ] **Single instance expectation:** no other `btcbot run` using same DB/account scope.

Quick checks:

```bash
# 1) Confirm no other run process (Linux/macOS example)
pgrep -af "btcbot run"

# 2) Confirm env file is loaded when using shell-based launch
# (or use your service manager equivalent)
set -a; source .env.pilot; set +a
```

---

## C) Stage-by-stage procedure

Use this as the canonical rollout path.

## Stage 0 — Observe-only / dry-run (no orders)

### Objective
Validate runtime behavior with **zero exchange write side effects**.

### Commands

```bash
btcbot doctor
```

Expected:
- `doctor_status=PASS` preferred.
- `doctor_status=WARN` can be acceptable only if warnings are understood/accepted.
- Any `FAIL` => do not proceed.

```bash
btcbot run --dry-run --loop --cycle-seconds 10 --max-cycles 60
```

### What to watch

- Decision events for stale market data (`market_data:stale` blocks).
- `reconcile_lag_ms` histogram behavior.
- `order_submit_latency_ms` histogram behavior (still useful even in dry-run pathing).
- `invalid_best_bid_count` trend (and legacy/deprecated `stale_market_data_rate` if still wired in your dashboards).

### Abort criteria and immediate actions

Abort Stage 0 if any of the following occurs:

- Repeated stale-market-data blocks over several consecutive cycles.
- Continuous growth in invalid best bid counts.
- Unexpected exceptions, repeated retries, or lock contention.

Actions:

1. Stop the process.
2. Keep `DRY_RUN=true`, `SAFE_MODE=true`, `KILL_SWITCH=true`.
3. Investigate market-data mode/freshness and clock sync before retry.

---

## Stage 1 — Read-only connectivity validation (no trading)

### Objective
Prove connectivity and runtime config coherence before arming.

### Commands

```bash
btcbot health
```

```bash
btcbot doctor
```

Expected:
- `btcbot health`: public API should be `OK` (or `SKIP` only in constrained environments).
- `btcbot doctor`: `PASS` preferred, or known/accepted WARN-only profile.

### Confirm market data mode/freshness settings

Validate these env values (and operator intent):

- `MARKET_DATA_MODE=rest|ws`
- `MAX_MARKET_DATA_AGE_MS=<strict threshold>`
- If using WS mode:
  - `BTCTURK_WS_ENABLED=true`
  - optional `WS_MARKET_DATA_REST_FALLBACK=true|false`

### Abort criteria

Stop and investigate if:

- `doctor` returns `FAIL`.
- `health` returns persistent `FAIL`.
- Mode/config mismatch (for example `MARKET_DATA_MODE=ws` but WS not enabled).

---

## Stage 2 — Single-order live canary (minimal notional)

### Objective
Run one tightly bounded live cycle with minimal economic risk.

### Arming requirements (all mandatory)

- `LIVE_TRADING=true`
- `LIVE_TRADING_ACK=I_UNDERSTAND`
- `DRY_RUN=false`
- `KILL_SWITCH=false`
- `SAFE_MODE=false`

### Minimal live config template (example)

```dotenv
# execution gates
LIVE_TRADING=true
LIVE_TRADING_ACK=I_UNDERSTAND
DRY_RUN=false
KILL_SWITCH=false
SAFE_MODE=false

# scope / universe
UNIVERSE_SYMBOLS=BTCTRY

# hard risk caps
MAX_ORDERS_PER_CYCLE=1
MAX_OPEN_ORDERS_PER_SYMBOL=1
NOTIONAL_CAP_TRY_PER_CYCLE=150
MAX_NOTIONAL_PER_ORDER_TRY=150
MIN_ORDER_NOTIONAL_TRY=10
TTL_SECONDS=30

# market data
MARKET_DATA_MODE=rest
MAX_MARKET_DATA_AGE_MS=3000

# persistence
STATE_DB_PATH=./var/btcbot_state.db
```

> Set `MIN_ORDER_NOTIONAL_TRY` at/above BTCTurk symbol minimums. If in doubt, use exchange rule metadata and choose a conservative value.

### Single-cycle command

```bash
btcbot canary once --symbol BTCTRY --notional-try 150 --ttl-seconds 30 --cycle-seconds 10
```

Legacy equivalent:

```bash
btcbot run --once
```

### Post-cycle verification

```bash
btcbot doctor --json
```

Then verify:

1. No new `FAIL` checks introduced.
2. No unresolved UNKNOWN lifecycle evidence in logs/records.
3. Open orders and balances are consistent with expected canary behavior:
   - check exchange open-orders view,
   - check exchange balance/position changes,
   - confirm local DB/state reflects the same outcome.

Optional supporting exports:

```bash
btcbot stage7-report --last 20
btcbot stage7-export --last 50 --format jsonl --out ./artifacts/stage7_canary.jsonl
```

### Abort / rollback actions

1. Immediately set `KILL_SWITCH=true`.
2. Run one controlled cycle to process protective logic:

```bash
btcbot run --once
```

3. If any order remains open unexpectedly, cancel manually in BTCTurk UI.
4. Revert to safe baseline: `SAFE_MODE=true`, `DRY_RUN=true`, `LIVE_TRADING=false`.

---

## Stage 3 — Bounded live canary loop (hard stops)

### Objective
Run short-lived live window with deterministic stop conditions.

### Command

```bash
btcbot canary loop --symbol BTCTRY --notional-try 150 --ttl-seconds 30 --cycle-seconds 10 --max-cycles 60
```

Legacy equivalent:

```bash
btcbot run --loop --cycle-seconds 10 --max-cycles 60
```

### Enforced caps and stop conditions

Keep Stage 2 hard caps in place (single symbol, low notional, strict TTL).

Stop immediately when any occurs:

- `doctor` produces `FAIL`.
- Stale-market-data blocks repeatedly (investigate feed/clock/mode before resume).
- Reject/latency SLO breach versus configured doctor thresholds.
- Safe-mode trigger or kill-switch activation.

Recommended periodic checks during loop:

```bash
btcbot doctor
btcbot stage7-report --last 20
btcbot stage7-alerts --last 50
```

### Operator decision tree (common failures)

- **Stale market data blocks**
  1. Stop loop.
  2. Verify `MARKET_DATA_MODE`, WS connectivity, `MAX_MARKET_DATA_AGE_MS`, clock sync.
  3. Resume only from Stage 0/1 after stabilization.

- **High rejects / latency deterioration**
  1. Stop loop.
  2. Set `KILL_SWITCH=true`.
  3. Review recent metrics/exports and exchange health.
  4. Resume in dry-run first.

- **Unknown order state / reconcile anomalies**
  1. Stop loop and set `KILL_SWITCH=true`.
  2. Reconcile via one controlled cycle and manual exchange checks.
  3. If unresolved, manual cancel + incident process.

---

## D) Monitoring and SLO interpretation

## Doctor status meaning

- **PASS**: no failing/warning checks; proceed to next gate.
- **WARN**: caution; proceed only if warnings are understood and bounded.
- **FAIL**: hard stop; do not continue live rollout.

Exit codes:
- `0` = PASS
- `1` = WARN
- `2` = FAIL

## Core SLO metrics (doctor window)

`doctor` evaluates (when DB metrics are available):

- `reject_rate`
- `fill_rate`
- `latency_p95_ms`
- `max_drawdown_ratio`

If DB path/metrics are missing, SLO coverage may be warn/skip-oriented; treat this as reduced confidence and avoid promoting to higher live stage until resolved.

## Minimal go/no-go matrix

| Condition | Action |
|---|---|
| `doctor=PASS`, no repeated stale blocks, canary behavior expected | **Go** to next stage |
| `doctor=WARN` only, warnings known/accepted, no instability trend | **Conditional go** (operator sign-off) |
| Any `doctor=FAIL` | **No-go**, stop and remediate |
| Repeated stale blocks / unknown lifecycle / rising reject+latency | **No-go**, rollback to dry-run/safe mode |

---

## E) Rollback / incident procedure

### Immediate controls

1. Set `KILL_SWITCH=true`.
2. Set `SAFE_MODE=true`.
3. Disable live arming (`LIVE_TRADING=false`, `DRY_RUN=true`).

### Order safety

- Run one controlled cycle if policy uses runtime cleanup paths:

```bash
btcbot run --once
```

- Confirm/cancel remaining open orders in BTCTurk UI if needed.

### Preserve evidence (do before restart loops)

```bash
btcbot doctor --json > ./artifacts/doctor_incident.json
btcbot stage7-export --last 200 --format jsonl --out ./artifacts/stage7_incident.jsonl
btcbot stage7-alerts --last 200 > ./artifacts/stage7_alerts_incident.txt
```

Also archive runtime logs and decision envelopes for the incident window.

---

## F) Appendix

## `.env.pilot` style skeleton (no real secrets)

```dotenv
# ===== BTCTurk credentials =====
BTCTURK_API_KEY=__REDACTED__
BTCTURK_API_SECRET=__REDACTED__

# ===== runtime safety gates =====
SAFE_MODE=true
DRY_RUN=true
KILL_SWITCH=true
LIVE_TRADING=false
LIVE_TRADING_ACK=

# ===== market data =====
MARKET_DATA_MODE=rest
MAX_MARKET_DATA_AGE_MS=3000
BTCTURK_WS_ENABLED=false
WS_MARKET_DATA_REST_FALLBACK=false

# ===== universe + risk =====
UNIVERSE_SYMBOLS=BTCTRY
MAX_ORDERS_PER_CYCLE=1
MAX_OPEN_ORDERS_PER_SYMBOL=1
MIN_ORDER_NOTIONAL_TRY=10
NOTIONAL_CAP_TRY_PER_CYCLE=150
MAX_NOTIONAL_PER_ORDER_TRY=150
TTL_SECONDS=30

# ===== state =====
STATE_DB_PATH=./var/btcbot_state.db
LOG_LEVEL=INFO
```

## Minimum safe config (baseline)

- `SAFE_MODE=true`
- `DRY_RUN=true`
- `KILL_SWITCH=true`
- `LIVE_TRADING=false`
- single-symbol universe
- strict notional caps
- short canary runtime windows (`--max-cycles`)

Use this baseline whenever uncertainty exists, then re-enter at Stage 0.
