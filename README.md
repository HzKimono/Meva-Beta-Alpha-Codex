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
cp .env.example .env
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
Copy-Item .env.example .env -Force

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

Copy `.env.example` to `.env` and adjust only what you need.

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
# doctor
$env:SYMBOLS="BTCTRY"
py -m btcbot.cli doctor --db .\btcbot_state.db --dataset .\data

# dry-run single cycle
py -m btcbot.cli stage4-run --dry-run --once

# dry-run loop (identical planning path, no side effects)
py -m btcbot.cli stage4-run --dry-run --loop --cycle-seconds 30 --max-cycles 20 --jitter-seconds 2

# live single cycle (all safety gates required)
$env:DRY_RUN="false"
$env:KILL_SWITCH="false"
$env:LIVE_TRADING="true"
$env:LIVE_TRADING_ACK="I_UNDERSTAND"
$env:BTCTURK_API_KEY="<key>"
$env:BTCTURK_API_SECRET="<secret>"
py -m btcbot.cli stage4-run --once

# live loop
py -m btcbot.cli stage4-run --loop --cycle-seconds 30 --jitter-seconds 2

# quality checks
py -m ruff check .
py -m ruff format --check .
py -m pytest -q
```

Notes:
- Startup logs include an explicit `arm_check` summary for each live gate.
- `--once` forces a single cycle (alias behavior for loop-capable commands).
- No DB migration is required for this update.

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
