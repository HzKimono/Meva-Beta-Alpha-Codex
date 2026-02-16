# Pilot Live Operations (BTCTurk)

This profile is conservative and keeps a cash reserve while using a dynamic TRY universe.

## Safe defaults and arming gates

Defaults remain production-safe:
- `KILL_SWITCH=true`
- `DRY_RUN=true`
- `LIVE_TRADING=false`

To arm live trading, all are required:
- `DRY_RUN=false`
- `KILL_SWITCH=false`
- `LIVE_TRADING=true`
- `LIVE_TRADING_ACK=I_UNDERSTAND`

## `.env.live` template (no secrets)

```dotenv
# Runtime mode
DRY_RUN=true
KILL_SWITCH=true
LIVE_TRADING=false
LIVE_TRADING_ACK=

# Pilot cash reserve / universe
TRY_CASH_TARGET=300
DYNAMIC_UNIVERSE_ENABLED=true
UNIVERSE_TOP_N=5

# Conservative caps
NOTIONAL_CAP_TRY_PER_CYCLE=1000
MAX_NOTIONAL_PER_ORDER_TRY=250
MIN_ORDER_NOTIONAL_TRY=100
MAX_ORDERS_PER_CYCLE=2

# Optional
STATE_DB_PATH=btcbot_state.db
LOG_LEVEL=INFO
```

## PowerShell commands

### Clean test run (Stage 7 test mode)

```powershell
$env:STAGE7_ENABLED="true"
py -m pytest -q
Remove-Item Env:STAGE7_ENABLED -ErrorAction SilentlyContinue
```

### Dry-run single cycle

```powershell
py -m btcbot.cli --env-file .env.live stage4-run --dry-run --once
```

### Live single-cycle (conservative)

```powershell
$env:DRY_RUN="false"
$env:KILL_SWITCH="false"
$env:LIVE_TRADING="true"
$env:LIVE_TRADING_ACK="I_UNDERSTAND"
$env:BTCTURK_API_KEY="<set-in-shell>"
$env:BTCTURK_API_SECRET="<set-in-shell>"
py -m btcbot.cli --env-file .env.live stage4-run --once
```

### Live loop (infinite)

```powershell
py -m btcbot.cli --env-file .env.live stage4-run --loop --cycle-seconds 30 --jitter-seconds 2 --max-cycles -1
```

`--max-cycles -1` means run until interrupted (`Ctrl+C`).
