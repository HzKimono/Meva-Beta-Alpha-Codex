# Production Operations Runbook (Safe Bot Operations)

## Operator Quick Start

1. Create venv and install dependencies.
2. Copy `.env.example` to `.env` and keep safe defaults (`DRY_RUN=true`, `KILL_SWITCH=true`, `LIVE_TRADING=false`).
3. Run doctor before any cycle.
4. Start with stage4 dry-run once, then loop.
5. Only arm live mode after explicit gate checklist passes.

Evidence: safe defaults and operator commands are documented in `README.md`; doctor command and behavior are in CLI/doctor service. (`README.md` lines ~41-43, 155-179; `src/btcbot/cli.py` lines ~328-349, 1063-1096; `src/btcbot/services/doctor.py` lines ~46-127)

---

## 1) Runtime modes

## 1.1 Paper/simulated vs live trading

- **Stage3 `run` mode**:
  - dry-run exchange uses `build_exchange_stage3(... force_dry_run=True)` and `DryRunExchangeClient`; live uses `BtcturkHttpClient`.
  - live side effects blocked unless policy allows (kill-switch off, not dry-run, live armed).
  - Enforcement points:
    - CLI policy block before cycle execution (`validate_live_side_effects_policy`).
    - per-action live guard in `ExecutionService._ensure_live_side_effects_allowed`.
  - Evidence: `src/btcbot/services/exchange_factory.py` (~17-68), `src/btcbot/cli.py` (~476-491), `src/btcbot/services/execution_service.py` (~279-281, 628-637).

- **Stage4 `stage4-run` mode**:
  - execution service enforces kill-switch and live-mode branching (`simulated` vs real submit/cancel).
  - live mode is `settings.is_live_trading_enabled() and not settings.dry_run`.
  - Evidence: `src/btcbot/services/execution_service_stage4.py` (~43-57, 136-153, 203-212).

- **Live arming contract**:
  - Settings validator requires `LIVE_TRADING_ACK=I_UNDERSTAND`, kill-switch off, and API key/secret when `LIVE_TRADING=true`.
  - Evidence: `src/btcbot/config.py` (~487-503, 535-536).

## 1.2 Dry-run / no-execution mode

- `DRY_RUN=true` + `KILL_SWITCH=true` is safe default.
- In dry-run, stage3 execution records “would_*” actions; stage4 records simulated submits/cancels; stage7 is dry-run only.
- Evidence: `.env.example` (~6-11), `src/btcbot/services/execution_service.py` (~169-171, 185-191, 283-311), `src/btcbot/services/execution_service_stage4.py` (~136-145, 203-207), `src/btcbot/cli.py` (~657-669).

## 1.3 Backtest/simulation mode

- `stage7-backtest` runs replay dataset cycles through `Stage7BacktestRunner` -> `Stage7SingleCycleDriver` -> `Stage7CycleRunner` using `ReplayExchangeClient`.
- Stage7 runtime command requires dry-run and `STAGE7_ENABLED=true`.
- Evidence: `src/btcbot/cli.py` (~824-880, 657-669), `src/btcbot/services/stage7_backtest_runner.py` (~32-71), `src/btcbot/services/stage7_single_cycle_driver.py` (~27-77).

---

## 2) Configuration contract

## 2.1 Config files

- `.env.example` (operator template/defaults).
- `.env` (runtime env-file loaded by Settings).
- `pyproject.toml` (tooling/deps/scripts).
- `Makefile` (quality command).
- `.github/workflows/ci.yml` (CI quality gates).
- Evidence: `src/btcbot/config.py` (~15-20), `.env.example`, `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`.

## 2.2 Environment variables (authoritative list)

Authoritative env contract is `Settings` aliases in `src/btcbot/config.py`.

### Required for live trading
- `BTCTURK_API_KEY`, `BTCTURK_API_SECRET`, `LIVE_TRADING=true`, `LIVE_TRADING_ACK=I_UNDERSTAND`, `KILL_SWITCH=false`, `DRY_RUN=false`.
- Validation evidence: `src/btcbot/config.py` (~495-503), `src/btcbot/services/trading_policy.py` (~12-24).

### Optional with defaults (all variables)
- Exchange/Gates: `BTCTURK_BASE_URL`, `KILL_SWITCH`, `DRY_RUN`, `LIVE_TRADING`, `LIVE_TRADING_ACK`, `STAGE7_ENABLED`.
- Core execution: `TARGET_TRY`, `OFFSET_BPS`, `TTL_SECONDS`, `MIN_ORDER_NOTIONAL_TRY`, `STATE_DB_PATH`, `DRY_RUN_TRY_BALANCE`, `MAX_ORDERS_PER_CYCLE`, `MAX_OPEN_ORDERS_PER_SYMBOL`, `COOLDOWN_SECONDS`, `NOTIONAL_CAP_TRY_PER_CYCLE`, `MIN_PROFIT_BPS`, `MAX_POSITION_TRY_PER_SYMBOL`, `ENABLE_AUTO_KILL_SWITCH`.
- Stage4 controls: `MAX_OPEN_ORDERS`, `MAX_POSITION_NOTIONAL_TRY`, `MAX_DAILY_LOSS_TRY`, `MAX_DRAWDOWN_PCT`, `FEE_BPS_MAKER`, `FEE_BPS_TAKER`, `SLIPPAGE_BPS_BUFFER`, `TRY_CASH_TARGET`, `TRY_CASH_MAX`, `RULES_CACHE_TTL_SEC`, `FILLS_POLL_LOOKBACK_MINUTES`, `STAGE4_BOOTSTRAP_INTENTS`.
- Stage7 strategy/risk/oms: `STAGE7_SLIPPAGE_BPS`, `STAGE7_FEES_BPS`, `STAGE7_MARK_PRICE_SOURCE`, `STAGE7_UNIVERSE_SIZE`, `STAGE7_UNIVERSE_QUOTE_CCY`, `STAGE7_UNIVERSE_WHITELIST`, `STAGE7_UNIVERSE_BLACKLIST`, `STAGE7_MIN_QUOTE_VOLUME_TRY`, `STAGE7_MAX_SPREAD_BPS`, `STAGE7_VOL_LOOKBACK`, `STAGE7_SCORE_WEIGHTS`, `STAGE7_ORDER_OFFSET_BPS`, `STAGE7_RULES_FALLBACK_TICK_SIZE`, `STAGE7_RULES_FALLBACK_LOT_SIZE`, `STAGE7_RULES_FALLBACK_MIN_NOTIONAL_TRY`, `STAGE7_RULES_SAFE_MIN_NOTIONAL_TRY`, `STAGE7_RULES_REQUIRE_METADATA`, `STAGE7_RULES_INVALID_METADATA_POLICY`, `STAGE7_MAX_DRAWDOWN_PCT`, `STAGE7_MAX_DAILY_LOSS_TRY`, `STAGE7_MAX_CONSECUTIVE_LOSSES`, `STAGE7_MAX_DATA_AGE_SEC`, `STAGE7_SPREAD_SPIKE_BPS`, `STAGE7_RISK_COOLDOWN_SEC`, `STAGE7_CONCENTRATION_TOP_N`, `STAGE7_LOSS_GUARDRAIL_MODE`, `STAGE7_SIM_REJECT_PROB_BPS`, `STAGE7_RATE_LIMIT_RPS`, `STAGE7_RATE_LIMIT_BURST`, `STAGE7_RETRY_MAX_ATTEMPTS`, `STAGE7_RETRY_BASE_DELAY_MS`, `STAGE7_RETRY_MAX_DELAY_MS`, `STAGE7_REJECT_SPIKE_THRESHOLD`, `STAGE7_RETRY_ALERT_THRESHOLD`.
- Risk/anomaly/global: `RISK_MAX_DAILY_DRAWDOWN_TRY`, `RISK_MAX_DRAWDOWN_TRY`, `RISK_MAX_GROSS_EXPOSURE_TRY`, `RISK_MAX_POSITION_PCT`, `RISK_MAX_ORDER_NOTIONAL_TRY`, `RISK_MIN_CASH_TRY`, `RISK_MAX_FEE_TRY_PER_DAY`, `STALE_MARKET_DATA_SECONDS`, `REJECT_SPIKE_THRESHOLD`, `LATENCY_SPIKE_MS`, `CURSOR_STALL_CYCLES`, `PNL_DIVERGENCE_TRY_WARN`, `PNL_DIVERGENCE_TRY_ERROR`, `DEGRADE_WARN_WINDOW_CYCLES`, `DEGRADE_WARN_THRESHOLD`, `DEGRADE_WARN_CODES_CSV`, `CLOCK_SKEW_SECONDS_THRESHOLD`, `LOG_LEVEL`.
- Universe/portfolio: `UNIVERSE_QUOTE_CURRENCY`, `UNIVERSE_MAX_SIZE`, `UNIVERSE_MIN_NOTIONAL_TRY`, `UNIVERSE_MAX_SPREAD_BPS`, `UNIVERSE_MAX_EXCHANGE_MIN_TOTAL_TRY`, `UNIVERSE_ALLOW_SYMBOLS`, `UNIVERSE_DENY_SYMBOLS`, `UNIVERSE_REQUIRE_ACTIVE`, `UNIVERSE_REQUIRE_TRY_QUOTE`, `SYMBOLS`, `PORTFOLIO_TARGETS`.

Alias evidence: `src/btcbot/config.py` (aliases throughout ~22-193).
Defaults evidence: `src/btcbot/config.py` (field defaults ~22-193), `.env.example` (~1-54).

## 2.3 Safe defaults

- Safe defaults are no live side effects (`KILL_SWITCH=true`, `DRY_RUN=true`, `LIVE_TRADING=false`, `STAGE7_ENABLED=false`).
- Evidence: `.env.example` (~6-11).

## 2.4 Validation points

- Field validators + model validator in `Settings` enforce range/enum/coherence constraints.
- Live-mode coherence enforced in `validate_stage7_safety` and `is_live_trading_enabled`.
- Doctor provides operator preflight checks for gates, dataset, exchange rules, DB path.
- Evidence: `src/btcbot/config.py` (~195-536), `src/btcbot/services/doctor.py` (~46-127, 130-220, 249-272).

---

## 3) Observability

## 3.1 Logging structure

- JSON logger includes `timestamp`, `level`, `logger`, `message`.
- Context propagation adds `run_id` and `cycle_id` via contextvars (`with_cycle_context`).
- Exception logs include `error_type`, `error_message`, `traceback`.
- Evidence: `src/btcbot/logging_utils.py` (~12-39), `src/btcbot/logging_context.py` (~7-31), `src/btcbot/services/stage7_cycle_runner.py` (~155-159).

## 3.2 Correlation IDs

- HTTP adapter attaches `X-Request-ID` to outbound requests.
- Stage7 lifecycle uses `run_id` + `cycle_id` and persists `run_id` in run metrics.
- Evidence: `src/btcbot/adapters/btcturk_http.py` (~202-208, 254-261), `src/btcbot/services/stage7_cycle_runner.py` (~644-646, 678-764), `src/btcbot/services/state_store.py` (~487-493, 628-760).

## 3.3 Metrics emitted

- Stage4 cycle metrics: fills_count, orders_submitted/canceled, rejects_count, fill_rate, avg_time_to_fill, slippage_bps_avg, fees/pnl/meta.
- Stage7 run metrics: mode_base/final, universe size, intents counts, OMS status counts, fills/ledger counts, equity/gross/net pnl, fees, slippage, drawdown, turnover, throttled/retry counters, latency timers.
- Evidence: `src/btcbot/services/metrics_service.py` (~14-117), `src/btcbot/services/stage7_cycle_runner.py` (~609-675, 753-764), `src/btcbot/services/state_store.py` (~415-493, 628-760, 783-972, 1464+ cycle_metrics schema).

## 3.4 Where logs/metrics are written

- Logs: stdout/stderr JSON via root stream handler.
- Metrics/state: SQLite tables (`cycle_metrics`, `stage7_run_metrics`, `stage7_ledger_metrics`, `stage7_cycle_trace`, `cycle_audit`).
- Evidence: `src/btcbot/logging_utils.py` (~58-66), `src/btcbot/services/state_store.py` (~345-493, 1413-1469).

## 3.5 Safe debug enablement

- Set `LOG_LEVEL=DEBUG`; optionally tune `HTTPX_LOG_LEVEL` and `HTTPCORE_LOG_LEVEL`.
- Adapter sanitizes request params/json to remove sensitive keys (`api_key`, `secret`, `signature`, etc.) before attaching to errors.
- Evidence: `src/btcbot/logging_utils.py` (~67-78), `src/btcbot/adapters/btcturk_http.py` (~136-155, 291-293, 307-309).

## 4.3 Edge-case handling validation

a) **Partial fills**
- Stage7 OMS explicitly emits `PARTIAL_FILL` then `FILLED` when slices >1. (`src/btcbot/services/oms_service.py` L341-L371)
- Stage3/4 ingest relies on exchange fills stream + dedupe by `fill_id`. (`src/btcbot/services/accounting_service_stage4.py` L37-L87, L98-L101)

b) **Fees in base/quote**
- Stage3 accounting ignores non-quote fees with warning (risk of understated fee burden). (`src/btcbot/accounting/accounting_service.py` L57-L69)
- Stage4 logs fee conversion missing for non-TRY fee asset and records audit note. (`src/btcbot/services/accounting_service_stage4.py` L109-L114, L167-L172)
- Stage7 currently books fees in TRY for simulated OMS fills. (`src/btcbot/services/stage7_cycle_runner.py` L479-L535)
- **GAP:** no implemented generic FX fee conversion pipeline for non-TRY fees in stage3/4 live accounting.

## 4) Operational safety

## 4.1 Single-instance lock strategy

- **Current state:** no explicit process-level singleton lock found.
- **What exists:** SQLite `BEGIN IMMEDIATE`, `busy_timeout`, WAL reduce write conflicts.
- **Operator policy:** run one active writer per DB path.
- **GAP:** add explicit lock file or DB advisory lock row at startup.
- Evidence: `src/btcbot/services/state_store.py` (~94-120).

## 4.2 DB locking / concurrency expectations

- SQLite with WAL and 5s busy timeout; transactions rollback on exception.
- Stage7/OMS and stage4 critical writes are wrapped in transactions.
- Evidence: `src/btcbot/services/state_store.py` (~89-127, 783-972), `src/btcbot/services/oms_service.py` (~376-379), `src/btcbot/services/stage4_cycle_runner.py` (~196-205, 545-547).

## 4.3 Retry/backoff standards and config

- Loop runner retries cycle_fn up to 3 attempts with exponential backoff.
- HTTP adapter retries timeout/429/5xx and transport errors up to 4 attempts with capped total wait.
- Stage7 OMS uses deterministic exponential backoff + jitter (`STAGE7_RETRY_*`).
- Evidence: `src/btcbot/cli.py` (~387-418), `src/btcbot/adapters/btcturk_http.py` (~49-53, 103-110, 201-217, 315-327), `src/btcbot/services/retry.py` (~19-58), `src/btcbot/config.py` (~131-133).

## 4.4 Rate-limit handling

- HTTP 429 handled in adapter retry with `Retry-After` parse support.
- Stage7 OMS token bucket (`STAGE7_RATE_LIMIT_RPS`, `STAGE7_RATE_LIMIT_BURST`) emits `THROTTLED` events when depleted.
- Evidence: `src/btcbot/adapters/btcturk_http.py` (~68-100, 201-217), `src/btcbot/services/oms_service.py` (~127-131, 200-215), `src/btcbot/services/rate_limiter.py` (~6-46), `src/btcbot/config.py` (~129-130).

---

## 5) Failure modes and incident playbooks (12)

Format: **Detection** → **Immediate mitigation** → **Recovery** → **Data to collect**.

1. **Exchange timeout / 5xx / transport error**
- Detection: adapter retry logs and eventual exceptions from `_get`/`_private_get`.
- Mitigation: force dry-run (`DRY_RUN=true`) and keep kill-switch on.
- Recovery: verify connectivity/base URL, rerun `health`, then `doctor`.
- Collect: logs with `error_type`, request path/method, request_id.
- Evidence: `src/btcbot/adapters/btcturk_http.py` (~103-110, 201-217, 315-327), `src/btcbot/cli.py` (~685-712).

2. **Partial fills appear stuck**
- Detection: stage7 OMS has PARTIAL without FILLED progression; open non-terminal orders persist.
- Mitigation: run reconcile paths (`refresh_order_lifecycle` / OMS reconcile).
- Recovery: execute one dry-run cycle; inspect `stage7_order_events` and `stage7_orders`.
- Collect: affected `client_order_id`, event sequence, timestamps.
- Evidence: `src/btcbot/services/oms_service.py` (~341-379, 381-414), `src/btcbot/services/execution_service.py` (~63-127).

3. **Order rejected by precision/lot-size/min-notional**
- Detection: reject reasons in stage4 records (`min_notional_violation`, missing rules) or stage3 validation errors.
- Mitigation: keep dry-run; inspect exchange rules resolution for symbol.
- Recovery: adjust symbol/rules config; rerun doctor and dry-run one cycle.
- Collect: symbol, price, qty, rules status, rejection reason.
- Evidence: `src/btcbot/services/execution_service_stage4.py` (~90-134), `src/btcbot/domain/models.py` (~300-323), `src/btcbot/services/doctor.py` (~173-211).

4. **DB locked**
- Detection: sqlite busy/locked errors and cycle failures.
- Mitigation: stop duplicate bot processes using same DB.
- Recovery: restart single instance; if persistent, move to fresh DB path for canary.
- Collect: process list, DB path, stack trace, lock timing.
- Evidence: `src/btcbot/services/state_store.py` (~94-120), `src/btcbot/cli.py` (~592-604).

5. **DB corrupted / unreadable**
- Detection: doctor DB path check fails; sqlite errors on startup.
- Mitigation: switch to backup DB file and run dry-run only.
- Recovery: sqlite integrity check offline, restore from backup.
- Collect: failing DB file, sqlite error text, recent filesystem events.
- Evidence: `src/btcbot/services/doctor.py` (~249-272), `src/btcbot/cli.py` (~686-694).

6. **Clock drift / stale market data anomalies**
- Detection: anomaly detector warnings and degrade mode transitions.
- Mitigation: set kill-switch true; avoid live mode.
- Recovery: fix host NTP, rerun dry-run cycle and verify anomaly clear.
- Collect: anomaly events table rows, host NTP status.
- Evidence: `src/btcbot/services/stage4_cycle_runner.py` (~109-118, 378-406, 499-521), `src/btcbot/config.py` (~151-165).

7. **Unexpected exception in strategy/cycle loop**
- Detection: `loop_cycle_failed` / cycle exception logs.
- Mitigation: keep kill-switch on, switch to `--once` for controlled repro.
- Recovery: run with dry-run and capture stack; bisect config changes.
- Collect: cycle_id, command, stack trace, last config.
- Evidence: `src/btcbot/cli.py` (~387-411, 592-604).

8. **Live trading accidentally unarmed/blocked**
- Detection: explicit policy block message (`KILL_SWITCH=true...`, `DRY_RUN=true...`, live not armed message).
- Mitigation: do not bypass; correct env intentionally.
- Recovery: set all required vars and rerun `doctor`.
- Collect: arm_check log payload, env snapshot (without secrets).
- Evidence: `src/btcbot/services/trading_policy.py` (~27-32), `src/btcbot/cli.py` (~454-473, 484-491).

9. **Risk mode forced to OBSERVE_ONLY/REDUCE_RISK_ONLY**
- Detection: risk decision logs + persisted risk decisions.
- Mitigation: keep non-live; investigate drawdown/exposure/liquidity triggers.
- Recovery: reduce risk params, rebalance exposure, wait cooldown expiry.
- Collect: `stage7_risk_decisions`, `risk_decisions`, latest run metrics.
- Evidence: `src/btcbot/services/stage7_risk_budget_service.py` (~50-114), `src/btcbot/services/risk_budget_service.py` (~52-79), `src/btcbot/services/state_store.py` (~566-580).

10. **Retry storm / rate-limit pressure**
- Detection: high `retry_count`, `retry_giveup_count`, `oms_throttled_count`, 429 logs.
- Mitigation: increase cycle interval, lower symbol count/order rate, keep dry-run.
- Recovery: tune `STAGE7_RATE_LIMIT_*` and retry settings conservatively.
- Collect: run metrics rows, OMS events timeline, adapter 429 logs.
- Evidence: `src/btcbot/services/stage7_cycle_runner.py` (~622-675, 753-764), `src/btcbot/services/oms_service.py` (~200-298), `src/btcbot/config.py` (~129-133).

11. **PnL divergence / suspicious accounting**
- Detection: anomaly codes and ledger metrics shifts.
- Mitigation: freeze live writes (`KILL_SWITCH=true`), export cycle diagnostics.
- Recovery: replay backtest window and parity compare deterministic outputs.
- Collect: `stage7_cycle_trace`, `stage7_ledger_metrics`, `ledger_events`, parity fingerprints.
- Evidence: `src/btcbot/services/stage4_cycle_runner.py` (~378-461), `src/btcbot/cli.py` (~883-936, 939-960), `src/btcbot/services/state_store.py` (~761-780).

12. **Negative equity / margin-call-like risk** *(spot-only approximation)*
- Detection: very low/negative equity in run metrics and drawdown breaches.
- Mitigation: immediate observe-only by kill-switch; stop live mode.
- Recovery: flatten exposure manually if needed, then resume dry-run.
- Collect: equity, drawdown, exposure metrics, positions.
- Evidence: `src/btcbot/services/ledger_service.py` (~244-257, 258-320), `src/btcbot/services/stage7_risk_budget_service.py` (~50-68), `src/btcbot/domain/risk_budget.py` (~52-67).

Note: no futures/leverage/funding code paths found; treat as spot bot. Search found no matches for `leverage|margin|futures|funding|interest|borrow`. (repository search command)

---

## 6) Commands

## 6.1 Local quality commands

- `make check`
- `python -m compileall -q src tests`
- `ruff format --check .`
- `ruff check .`
- `python -m pytest -q`
- `python scripts/guard_multiline.py`

Evidence: `Makefile` (~1-8), `.github/workflows/ci.yml` (~29-42), `README.md` (~105-113).

## 6.2 Production run commands (safe to live progression)

- Preflight:
  - `python -m btcbot.cli doctor --db ./btcbot_state.db --dataset ./data/replay`
  - `python -m btcbot.cli health`
- Dry-run canary:
  - `python -m btcbot.cli stage4-run --dry-run --once`
  - `python -m btcbot.cli stage4-run --dry-run --loop --cycle-seconds 30 --max-cycles 20 --jitter-seconds 2`
- Live single cycle (only after gates):
  - set env: `DRY_RUN=false`, `KILL_SWITCH=false`, `LIVE_TRADING=true`, `LIVE_TRADING_ACK=I_UNDERSTAND`, plus keys
  - run: `python -m btcbot.cli stage4-run --once`

Evidence: `README.md` (~155-179), `src/btcbot/cli.py` (~260-268, 607-654, 685-713, 1063-1096).

## 6.3 Replay a cycle safely

- Initialize dataset: `python -m btcbot.cli replay-init --dataset ./data/replay --seed 123`
- Backtest deterministic window: `python -m btcbot.cli stage7-backtest --dataset ./data/replay --out ./backtest.db --start ... --end ... --step-seconds 60 --seed 123`
- Compare parity across two runs: `python -m btcbot.cli stage7-parity --out-a ./a.db --out-b ./b.db --start ... --end ... --include-adaptation`

Evidence: `docs/RUNBOOK.md` (~22-44), `src/btcbot/cli.py` (~824-936).

## 6.4 Export diagnostics

- Stage7 metrics report: `python -m btcbot.cli stage7-report --last 50`
- Export metrics: `python -m btcbot.cli stage7-export --last 50 --format jsonl --out ./metrics.jsonl`
- Backtest export: `python -m btcbot.cli stage7-backtest-export --db ./backtest.db --last 100 --format csv --out ./backtest.csv`
- DB counts: `python -m btcbot.cli stage7-db-count --db ./backtest.db`

Evidence: `src/btcbot/cli.py` (~280-286, 751-760, 765-960, 995-1017).

---

## Incident Checklist (concise)

1. Confirm mode/gates from latest `arm_check` log (`dry_run`, `kill_switch`, `live_trading`, `ack`).
2. Flip to safe state immediately: `KILL_SWITCH=true`, `DRY_RUN=true`.
3. Run `doctor` and `health`.
4. Capture last 100 run metrics + cycle trace rows.
5. Capture order/ledger event history for impacted `cycle_id` / `client_order_id`.
6. Check DB lock/contention (single active writer).
7. If exchange-side issue: preserve request_id/error_type logs.
8. Reproduce in dry-run `--once`.
9. If accounting issue: run replay backtest and parity on same window.
10. Do not re-arm live until root cause + mitigation validated in dry-run.

Evidence pointers: `src/btcbot/cli.py` (~454-473, 685-713, 751-760, 824-936), `src/btcbot/services/state_store.py` (~761-780, 783-972, 2411-2441), `src/btcbot/adapters/btcturk_http.py` (~202-208, 254-261).
