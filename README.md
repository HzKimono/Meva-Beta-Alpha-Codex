# Meva Beta Stage 3 (BTCTurk Spot Portfolio Bot)

Production-oriented Stage 3 trading bot architecture for BTCTurk spot markets with deterministic tests and strong safety defaults.

## Scope (Stage 3 default)

This repository is **Stage 3 by default**. The runtime pipeline is:

`MarketData + Portfolio + Reconcile/Fills -> Accounting -> Strategy -> Risk -> Execution`

At a high level:
- **Adapters** talk to BTCTurk HTTP APIs (public + private).
- **Services** orchestrate market reads, accounting refresh, strategy generation, risk filtering, and execution.
- **Domain** models provide typed contracts and symbol/rules normalization.
- **State store** (SQLite) provides idempotent cycle state and persistence.
- **Risk/Execution** enforce live-side-effect gates and runtime constraints.

## Triple-gate safety semantics (must understand)

The bot uses three independent safety controls:

1. `DRY_RUN=true`
   - Simulation mode only.
   - No live writes (no real submit/cancel side effects).
2. `KILL_SWITCH=true`
   - Blocks cancellations and order placement.
   - Planning/logging/accounting still run for observability.
3. Live arming requirement
   - Live writes require **all** of:
     - `DRY_RUN=false`
     - `KILL_SWITCH=false`
     - `LIVE_TRADING=true`
     - `LIVE_TRADING_ACK=I_UNDERSTAND`

If any live gate is not satisfied, execution remains blocked.

## Operational modes

| Mode | DRY_RUN | KILL_SWITCH | LIVE_TRADING | LIVE_TRADING_ACK | Behavior |
|---|---:|---:|---:|---|---|
| Safe default | 1 | 1 | 0 | empty | Planning/logging only; no live side effects |
| Dry-run simulated orders | 1 | 0 | 0 | empty | Simulated order flow; no live side effects |
| Live armed | 0 | 0 | 1 | `I_UNDERSTAND` | Live submit/cancel allowed |

## Setup

**Python 3.12+ required** (project metadata: `requires-python = ">=3.12"`).

### Linux/macOS

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env.live
```

### Windows PowerShell deterministic run

Use this to avoid ambient machine env differences:

```powershell
# 1) Enter repo + venv
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

# 2) Clear relevant ambient vars for deterministic behavior
$vars = @(
  "BTCTURK_API_KEY","BTCTURK_API_SECRET","BTCTURK_BASE_URL",
  "KILL_SWITCH","DRY_RUN","LIVE_TRADING","LIVE_TRADING_ACK",
  "TARGET_TRY","OFFSET_BPS","TTL_SECONDS","MIN_ORDER_NOTIONAL_TRY",
  "STATE_DB_PATH","DRY_RUN_TRY_BALANCE",
  "MAX_ORDERS_PER_CYCLE","MAX_OPEN_ORDERS_PER_SYMBOL","COOLDOWN_SECONDS",
  "NOTIONAL_CAP_TRY_PER_CYCLE","MIN_PROFIT_BPS","MAX_POSITION_TRY_PER_SYMBOL",
  "ENABLE_AUTO_KILL_SWITCH","LOG_LEVEL","SYMBOLS"
)
foreach ($v in $vars) { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }

# 3) Fresh config
Copy-Item .env.example .env.live -Force

# 4) Stage 3 acceptance commands
python -m pytest -q
python -m btcbot.cli health
python -m btcbot.cli run --dry-run
```

## Common commands

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
ruff format --check .
ruff check .
python -m pytest -q
python -m btcbot.cli health
python -m btcbot.cli run --dry-run
```

## Quality gates (CI/local)

```bash
python scripts/guard_multiline.py
ruff format --check .
ruff check .
python -m compileall src tests
python -m pytest -q
```

## Configuration

Copy `.env.example` to `.env.live` and adjust only what you need.

Use `--env-file .env.live` explicitly in CLI commands when you want to pin a specific runtime profile.

Key notes:
- `SYMBOLS` accepts either JSON list (recommended) or CSV.
- `LOG_LEVEL` defaults to `INFO`.
- Public API health uses `/api/v2/server/exchangeinfo`.
- Market orderbook reads use `/api/v2/orderbook?pairSymbol=...`.

## Known limitations (Stage 3)

- Positions/PnL are best-effort and depend on available trade/fill context.
- Mark pricing is based on best-bid snapshots from orderbook data.
- Exchange rules fallback defaults are used only when exchange info/rules are unavailable.
- This stage does not promise Stage 4 features (advanced portfolio analytics, multi-venue routing, etc.).


## Stage 4 additions

Cash buffer allocation policy (Stage 4):
- `TRY_CASH_TARGET` (default `300`) is reserved cash.
- Buy allocations use only `max(0, cash_try - TRY_CASH_TARGET)` (optionally capped by `TRY_CASH_MAX`) with fee buffer (`ALLOCATION_FEE_BUFFER_BPS`) so planned buys do not drive remaining cash below target.
- Account snapshots read private balances; when credentials/auth fail, logs include `missing_private_data` and fallback cash is used for deterministic dry-run planning.


Stage 4 modules are available for controlled live trading lifecycle/accounting/risk flows. See `docs/stage4.md` for canary checklist, idempotency model (`client_order_id`/`fill_id`), env vars, and current limitations.


## Stage 4 runtime (CLI)

Run Stage 4 directly via the dedicated subcommand:

```bash
python -m btcbot.cli stage4-run --dry-run
```

To arm Stage 4 live writes, all of the following must be set:

- `DRY_RUN=false`
- `KILL_SWITCH=false`
- `LIVE_TRADING=true`
- `LIVE_TRADING_ACK=I_UNDERSTAND`

Canary recommendation: keep low limits (`MAX_OPEN_ORDERS`, `MAX_POSITION_NOTIONAL_TRY`) and start in dry-run first.

## Operator quickstart (Windows PowerShell)

```powershell
Start-Transcript -Path .\logs\btcbot-session.txt -Force

# common env overrides
$env:TRY_CASH_TARGET="300"
$env:UNIVERSE_SYMBOLS="BTCTRY,ETHTRY,SOLTRY,XRPTTRY,ADATRY"
$env:UNIVERSE_AUTO_CORRECT="false" # optional typo auto-fix mode
$env:DRY_RUN="true"
$env:KILL_SWITCH="true"
$env:LOG_LEVEL="DEBUG"

# doctor (shows effective universe, source, rejected symbols, suggestions)
py -m btcbot.cli doctor --db .\btcbot_state.db --dataset .\data

# optional: enable auto-correct for obvious single-match typos
$env:UNIVERSE_AUTO_CORRECT="true"
py -m btcbot.cli doctor --db .\btcbot_state.db

# health snapshot
py -m btcbot.cli health

# dry-run single cycle
py -m btcbot.cli run --dry-run --once

# dry-run loop
py -m btcbot.cli run --dry-run --loop --sleep-seconds 30 --max-cycles 20 --jitter-seconds 2

# dry-run infinite loop
py -m btcbot.cli run --dry-run --loop --sleep-seconds 30 --max-cycles -1 --jitter-seconds 2

Stop-Transcript
```

Notes:
- Startup logs include an explicit `arm_check` summary for each live gate.
- `--once` forces a single cycle (alias behavior for loop-capable commands).
- `--max-cycles -1` runs continuously until interrupted; `--max-cycles 0` is invalid.
- No DB migration is required for this update.

## Pilot live profile (Top-5 TRY, conservative caps)

1. Copy `.env.pilot.example` to `.env.live` and set only credentials (`BTCTURK_API_KEY`, `BTCTURK_API_SECRET`).
2. Verify effective config values before running:

```bash
python -m btcbot.cli health
```

3. Start continuous pilot loop:

```bash
python -m btcbot.cli run --loop --cycle-seconds 30 --jitter-seconds 2 --max-cycles -1
```

The pilot profile keeps `TRY_CASH_TARGET=300`, limits to Top-5 universe symbols, and applies conservative cycle/order notional caps.

## Stage 7 backtest/parity quick checks

```powershell
python -m btcbot.cli stage7-backtest --dataset .\data --out .\backtest.db --start 2024-01-01T00:00:00Z --end 2024-01-01T01:00:00Z --step-seconds 60 --seed 123 --include-adaptation
python -m btcbot.cli stage7-parity --out-a .\run_a.db --out-b .\run_b.db --start 2024-01-01T00:00:00Z --end 2024-01-01T01:00:00Z --include-adaptation
python -m btcbot.cli doctor --db .\backtest.db --dataset .\data
```

`stage7-run` requires `STAGE7_ENABLED=true` and `--dry-run`; no live trading side effects are introduced.

### DB path configuration (Stage 7 CLI)

- Stage 7 commands that read/write the state DB accept `--db` (for example `stage7-run`, `stage7-report`, `stage7-export`, `stage7-alerts`, `stage7-backtest-report`, `stage7-db-count`).
- If `--db` is omitted, they fall back to `STATE_DB_PATH`.
- If neither is provided, the CLI prints an actionable error with an example command.

SQLite inspection (PowerShell-friendly; avoids bash heredoc syntax):

```powershell
@'
import sqlite3
conn = sqlite3.connect(r".\backtest.db")
for table in ("stage7_cycle_trace", "stage7_param_changes", "stage7_params_checkpoints"):
    print(table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
conn.close()
'@ | python
```

## Changelog

- ✅ Stage 3 acceptance achieved: strategy/risk/accounting pipeline integrated, deterministic safety gates enforced, and CI quality gates green.


## Quality gates (single command)

```bash
make check
```

This runs compile checks, formatting/linting, tests, and guard scripts in the same order used by CI.


## Exchange rules min-notional derivation

- The rules normalizer checks multiple BTCTurk variants (`minTotalAmount`, `minExchangeValue`, `minNotional`, `minQuoteAmount`, etc.) across top-level and filter/constraint payloads.
- If no reliable min-notional exists for TRY-quote pairs, it applies a conservative safe floor (`STAGE7_RULES_SAFE_MIN_NOTIONAL_TRY`, default `100`) and still enforces reject+continue semantics.
- Non-TRY pairs without reliable min-notional remain explicit `invalid_metadata` and are rejected safely.

### Optional fixture capture (dev)

```powershell
python scripts/capture_exchangeinfo_fixture.py
```

This writes a sanitized payload to `tests/fixtures/btcturk_exchangeinfo_live_capture.json` for parser regression tests.


## Reproducible builds
- Python dependencies are pinned in `pyproject.toml` and `constraints.txt`.
- Build the container image with a multi-stage Dockerfile:
  - `docker build -t btcbot:local .`
- Local orchestration:
  - `docker compose up --build`
