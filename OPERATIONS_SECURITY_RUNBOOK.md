# OPERATIONS & SECURITY RUNBOOK

## A) Deployment modes found and exact run steps

### A.1 Local Python (venv)
**Evidence**: repository setup and command examples in `README.md`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env.live
python -m btcbot.cli health
python -m btcbot.cli run --dry-run
```

### A.2 Docker image
**Evidence**: `Dockerfile` (`ENTRYPOINT ["btcbot"]`, `CMD ["run", "--once"]`).

```bash
docker build -t btcbot:local .
docker run --rm --env-file .env.live -v "$PWD:/app" btcbot:local run --dry-run --once
```

### A.3 Docker Compose
**Evidence**: `docker-compose.yml` (`env_file: .env.live`, volume `btcbot-data:/data`, command loop).

```bash
docker compose up --build
```

### A.4 VPS/systemd mode
- **VPS**: implied by runbook/ops docs but no dedicated provision script found.
- **systemd**: **UNKNOWN** (no unit file in repository). To confirm, check deployment repo/host for `*.service`.

### A.5 Live trading arming (P0-sensitive)
**Evidence**: `README.md`, `docs/RUNBOOK.md`, and runtime checks in `src/btcbot/services/trading_policy.py` and `src/btcbot/cli.py`.

Live side effects require **all**:
- `DRY_RUN=false`
- `KILL_SWITCH=false`
- `LIVE_TRADING=true`
- `LIVE_TRADING_ACK=I_UNDERSTAND`

**P0**: Any accidental env/profile setting that disables these guards can place unintended orders.

---

## B) Config & secret handling

### B.1 Config sources and keys
- Settings model is `src/btcbot/config.py::Settings` (env-driven, default `.env.live`).
- Templates:
  - `.env.example` (safe defaults, dry-run + kill switch enabled),
  - `.env.pilot.example` (live profile template, arming enabled).

### B.2 Secret loading chain
**Evidence**: `src/btcbot/security/secrets.py`.
- `build_default_provider`: env first, optional dotenv second.
- `inject_runtime_secrets`: injects keys into `os.environ` only if missing.
- `validate_secret_controls`: enforces scope policy (`withdraw` forbidden), required scopes, rotation timestamp and max age.

### B.3 Redaction and logging safety
**Evidence**: `src/btcbot/security/redaction.py`, `src/btcbot/logging_utils.py`, adapter sanitization in `src/btcbot/adapters/btcturk_http.py`.
- Structured logs are JSON via `JsonFormatter`.
- `redact_value` and `redact_text` mask sensitive fields and token-like substrings.
- BTCTurk adapter sanitizes request headers/json/params before logging error payloads.

### B.4 Key permissions and storage requirements
**P0 controls (required):**
1. Never commit `.env.live` with real keys.
2. Restrict env file permissions (e.g., `chmod 600 .env.live`) on Linux hosts.
3. Use keys scoped to `read,trade`; never include `withdraw` (`BTCTURK_API_SCOPES`).
4. Track and enforce rotation (`BTCTURK_SECRET_ROTATED_AT`, `BTCTURK_SECRET_MAX_AGE_DAYS`).
5. Keep `SAFE_MODE=true` during key rotation and first boot after changes.

### B.5 P0 leak vectors to monitor
- P0: keys in shell history / CI logs / copied `.env` artifacts.
- P0: raw exception logging from external wrappers not using project redaction utilities.
- P0: shared host account permissions allowing other users to read `.env.live` or sqlite files.

---

## C) Observability

### C.1 Logs
- Format: JSON (`logging_utils.JsonFormatter`).
- Level: `LOG_LEVEL`, with optional `HTTPX_LOG_LEVEL`, `HTTPCORE_LOG_LEVEL`.
- Context fields: `run_id`, `cycle_id`, `client_order_id`, `order_id`, `symbol`.

### C.2 Rotation
- App writes to stdout/stderr stream handler only.
- File rotation is **external** (Docker logging driver / system log collector).
- Built-in file-rotation handler: **not found**.

### C.3 Metrics & tracing
**Evidence**: `docs/RUNBOOK.md`, `docs/SLO.md`, `src/btcbot/observability.py`, adapter instrumentation.
- Config:
  - `OBSERVABILITY_ENABLED`
  - `OBSERVABILITY_METRICS_EXPORTER` (`none|otlp|prometheus`)
  - `OBSERVABILITY_OTLP_ENDPOINT`
  - `OBSERVABILITY_PROMETHEUS_PORT`
- Key signals:
  - `ws_reconnect_rate`, `rest_429_rate`, `rest_retry_rate`,
  - `stale_market_data_rate`, `reconcile_lag_ms`,
  - `order_submit_latency_ms`, `cancel_latency_ms`,
  - `circuit_breaker_state`.

### C.4 Health checks
- Command-level: `python -m btcbot.cli health`, `python -m btcbot.cli doctor`.
- HTTP health endpoint service: **UNKNOWN** (none in inspected code).

### C.5 Alert hooks
- Threshold definitions exist in `docs/SLO.md`.
- Built-in pager/webhook integration destination is **UNKNOWN** (not configured in repo).

---

## D) Incident playbooks

### D.1 Exchange outage / API instability
**Symptoms**: high 5xx/timeouts, failed health, elevated retries.

**Immediate actions (P0-safe):**
1. Set `SAFE_MODE=true` and restart.
2. Verify with `python -m btcbot.cli health`.
3. Reduce pressure: tune `BTCTURK_RATE_LIMIT_RPS`, `BTCTURK_RATE_LIMIT_BURST`.
4. Increase retry spacing: `BTCTURK_REST_BASE_DELAY_MS`, `BTCTURK_REST_MAX_DELAY_MS`.
5. Resume dry-run first, then re-arm only after sustained stability.

### D.2 Runaway orders / unintended order placement (P0)
**Symptoms**: unexpected order bursts, abnormal submits/cancels.

**Immediate actions (P0):**
1. Force `SAFE_MODE=true` (or `KILL_SWITCH=true`) and restart.
2. Confirm logs show block reasons from `validate_live_side_effects_policy`.
3. Inspect state DB for action dedupe/order metadata (`StateStore` tables).
4. Validate env values and remove stale live profile files.
5. Keep dry-run until root cause and reconciliation complete.

### D.3 Balance mismatch / reconciliation drift
**Symptoms**: local positions/balances diverge from exchange views.

**Actions:**
1. Run one recovery cycle with safe mode: startup recovery invokes lifecycle + fills refresh.
2. Run `doctor` and inspect recent `cycle_audit` / risk metrics tables.
3. Reconcile open orders via exchange and local state; import external/open mismatches using Stage4 reconcile path if applicable.
4. If unresolved unknown orders persist, maintain observe-only and perform manual exchange audit.

### D.4 High error rate (429/transport/WS drops)
**Symptoms**: spikes in `rest_429_rate`, `rest_retry_rate`, `ws_reconnect_rate`.

**Actions:**
1. Set `SAFE_MODE=true`.
2. Check network/DNS/TLS path and exchange status.
3. Tune retry/rate-limit and WS idle/backoff thresholds.
4. Resume in dry-run and monitor SLO windows before live.

### D.5 Negative PnL spike / drawdown breach
**Symptoms**: daily loss threshold hit, drawdown near halt limit, risk mode downgrade.

**Actions:**
1. Confirm risk decision path (`max_daily_loss`, `max_drawdown`, or Stage7 mode transitions).
2. Keep observe-only mode until data/accounting integrity is validated.
3. Validate marks and fee assumptions (especially non-TRY/non-quote fee caveats).
4. Reduce notional caps and open-order limits for controlled restart.

---

## E) Hardening checklist

### E.1 Build/dependency hardening
- [x] Dependency pinning in `pyproject.toml` and `constraints.txt`.
- [x] CI static checks: ruff, mypy, compileall, pytest, bandit (`.github/workflows/ci.yml`).
- [ ] P0 recommended: add `pip-audit`/OSV scanning in CI.
- [ ] P0 recommended: add secret scanning in CI (gitleaks/trufflehog equivalent).
- [ ] P1 recommended: generate SBOM and sign build artifacts.

### E.2 Runtime hardening
- [x] Non-root container runtime user in `Dockerfile` (`USER btcbot`).
- [x] Safe defaults in `.env.example` (`SAFE_MODE=true`, `DRY_RUN=true`, `KILL_SWITCH=true`).
- [x] Process lock to prevent duplicate instance (`services/process_lock.py`).
- [ ] P0 recommended: enforce file permissions (`.env.live`, DB path) in deployment scripts.
- [ ] P0 recommended: separate prod and test API keys with minimal scopes.

### E.3 Key rotation procedure (operational)
**Evidence**: `docs/RUNBOOK.md` + secret controls in `security/secrets.py`.
1. Set `SAFE_MODE=true`.
2. Rotate `BTCTURK_API_KEY` and `BTCTURK_API_SECRET`.
3. Update `BTCTURK_SECRET_ROTATED_AT` to current ISO-8601 time.
4. Restart service; run `doctor` and `health`.
5. Verify secret validation logs report no errors.
6. Resume dry-run; re-arm live only after stability window.

### E.4 P0 unsafe configurations to block in change reviews
1. `DRY_RUN=false` with `LIVE_TRADING=true` in non-production test environments.
2. Any config/profile that sets arming flags without explicit operator acknowledgment process.
3. API scopes including `withdraw`.
4. Logging sinks that bypass project redaction.
5. Unprotected env files, shared writable config directories, or leaked CI artifacts with secrets.

---

## Quick command appendix

```bash
# safety checks
python -m btcbot.cli health
python -m btcbot.cli doctor --json

# safe startup
python -m btcbot.cli run --dry-run --once

# stage-specific dry runs
python -m btcbot.cli stage4-run --dry-run --once
python -m btcbot.cli stage7-run --dry-run

# docker
docker build -t btcbot:local .
docker compose up --build
```

(Use lowercase `docker` command in shell; uppercase shown here only if copied from stylized docs.)
