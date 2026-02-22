# BTC Turk Bot Architecture (Two-Process, SQLite-Isolated)

## Layers & Responsibilities
- **CLI Layer (`btcbot.cli`)**
  - Exposes `run` (LIVE TRADE loop) and `health` (MONITOR checks).
  - Parses command args and starts the correct application service.
  - Does not contain trading/risk/persistence logic.

- **Configuration Layer (`btcbot.config.settings`)**
  - Loads environment-only configuration (dotenv disabled).
  - Validates required env vars (`APP_ROLE`, `STATE_DB_PATH`, `LIVE_TRADING`, `SAFE_MODE`, `KILL_SWITCH`, API keys).
  - Enforces role profile:
    - LIVE TRADE: `LIVE_TRADING=true` + explicit acknowledgement flag.
    - MONITOR: `LIVE_TRADING=false`, `SAFE_MODE=true`, `KILL_SWITCH=true`.

- **Application Layer (`btcbot.services.*`)**
  - Orchestrates use-cases (`RunLoopService`, `HealthService`).
  - Coordinates strategy, risk, execution, and persistence through interfaces.
  - Owns per-cycle transaction boundaries.

- **Domain Layer (`btcbot.domain`, `btcbot.strategy`, `btcbot.risk`)**
  - Pure, typed business logic and policies.
  - `strategy`: signal generation from normalized market state.
  - `risk`: allow/block decisions, sizing caps, kill-switch policies.
  - No direct I/O, no DB/network imports.

- **Market/Exchange Adapter Layer (`btcbot.market_data`, `btcbot.exchange`)**
  - Market data adapter: fetches/orderbooks/trades/candles snapshots.
  - Exchange adapter: authenticated BTC Turk order APIs (submit/cancel/query).
  - Translates external DTOs into internal domain models.

- **Execution Layer (`btcbot.execution`)**
  - Sole module that can call exchange order side effects.
  - Accepts approved intents only (post-risk).
  - Emits structured execution events for persistence and observability.

- **Persistence Layer (`btcbot.persistence.sqlite`)**
  - SQLite connection/session factory and repositories.
  - Implements Unit of Work (single commit point per cycle).
  - Enforces DB ownership metadata and role/path guard checks.

- **Observability Layer (`btcbot.observability`)**
  - Structured logs, metrics, traces, and audit events.
  - Read-only regarding strategy/risk decisions (no decision authority).

## Allowed Dependencies
- `btcbot.cli` -> `btcbot.config.settings`, `btcbot.services`, `btcbot.observability`
- `btcbot.services` -> `btcbot.strategy`, `btcbot.risk`, `btcbot.execution`, `btcbot.market_data`, `btcbot.persistence`, `btcbot.domain`, `btcbot.observability`
- `btcbot.strategy` -> `btcbot.domain`
- `btcbot.risk` -> `btcbot.domain`
- `btcbot.execution` -> `btcbot.domain`, `btcbot.exchange`, `btcbot.observability`
- `btcbot.market_data` -> `btcbot.domain`, `btcbot.exchange`
- `btcbot.persistence` -> `btcbot.domain`, `btcbot.config.settings`
- `btcbot.exchange` -> (external SDK/http libs only), `btcbot.domain` for mappers
- `btcbot.observability` -> (external logging/metrics libs only)

**Forbidden (explicit):**
- No module may import `btcbot.cli`.
- `btcbot.domain` must import nothing from `services/execution/persistence/exchange/cli`.
- `btcbot.strategy` and `btcbot.risk` must not import persistence or exchange modules.
- `btcbot.exchange` must not import persistence.
- `btcbot.persistence` must not import strategy/risk/execution (repositories stay generic).
- Cycles are forbidden across all packages (enforce with import-linter / CI check).

## LIVE TRADE Cycle Flow
1. **Startup inputs**
   - `btcbot.cli run` loads env config: `APP_ROLE=live`, `STATE_DB_PATH=<live_db>`, `LIVE_TRADING=true`, acknowledgement flag, API credentials, cycle seconds.
   - App boot fails fast if any role invariant is violated.

2. **Cycle begins (orchestrator)**
   - `RunLoopService` opens Unit of Work and correlation ID for this cycle.
   - Reads last known state from persistence read repositories.

3. **Market inputs**
   - `market_data` fetches latest BTC Turk market snapshot(s) and normalizes to domain models.

4. **Decision: signal generation**
   - `strategy` computes signal(s): e.g., `BUY`, `SELL`, `HOLD`, with confidence/size hints.

5. **Decision: risk gate**
   - `risk` evaluates limits and safety flags (exposure, cooldown, drawdown, kill-switch).
   - Outputs `ApprovedOrderIntent` or rejection reason(s).

6. **Side effect: execution**
   - If approved and trading is armed, `execution` submits/cancels orders via `exchange`.
   - Receives exchange ACK/status and maps to domain execution events.

7. **Side effect: persistence (single writer path)**
   - Application service persists cycle record, signal, risk result, order intents, fills/status events through persistence repositories.
   - Unit of Work commits once; rollback on error.

8. **Side effect: observability**
   - Emit structured logs/metrics/traces/audit with cycle ID and DB path fingerprint.

9. **Cycle end**
   - Sleep `--cycle-seconds` and repeat.

## MONITOR Flow
1. **Startup inputs**
   - `btcbot.cli health` loads env config: `APP_ROLE=monitor`, `STATE_DB_PATH=<monitor_db>`, `LIVE_TRADING=false`, `SAFE_MODE=true`, `KILL_SWITCH=true`.
   - Boot fails if any monitor safety flag is not set exactly.

2. **Health orchestration**
   - `HealthService` performs read-only checks: config validity, DB connectivity/schema, exchange reachability, data freshness, queue/backlog status.

3. **Decision: health status**
   - Produces `OK/WARN/FAIL` with typed reasons and remediation hints.

4. **Side effects (safe only)**
   - Writes health snapshots/events to **monitor DB only** (optional but allowed).
   - Emits logs/metrics/audit.
   - **Never calls execution submit/cancel**; execution provider in monitor mode is a `NoopExecution` that hard-fails on trade attempts.

5. **Return/exit behavior**
   - Returns status code suitable for scheduler/supervisor alerts.

## DB Safety Rules
- **Separate DB paths are mandatory**
  - LIVE and MONITOR must use distinct absolute `STATE_DB_PATH` values.
  - Reject startup if paths are equal or resolve to same inode/file.

- **Role-bound DB ownership**
  - On DB initialization, write immutable metadata table: `db_role`, `created_by`, `db_uuid`.
  - LIVE process can open only DBs tagged `db_role=live`; MONITOR only `db_role=monitor`.

- **Single-writer rule (code-level)**
  - Only `btcbot.services` may invoke write repositories through `UnitOfWork`.
  - Strategy/risk/exchange/market_data modules are write-blind.
  - Direct `sqlite3` usage outside persistence package is prohibited by lint rule.

- **Single-writer rule (process-level)**
  - Acquire an OS file lock per DB at startup (`<db>.lock`) containing PID + role + hostname.
  - If lock held by another live process for same DB, refuse to start.
  - Use SQLite WAL + busy timeout, but still keep one bot writer process per DB.

- **Hard trade guardrails**
  - Execution checks a `TradingArmedPolicy` each call: requires role=live, `LIVE_TRADING=true`, acknowledged=true, `SAFE_MODE=false`, `KILL_SWITCH=false`.
  - Any mismatch raises a non-retryable `TradeBlockedError` before network I/O.

- **Auditability**
  - Every DB write includes `process_role`, `process_instance_id`, `cycle_id`, and config hash.
  - Makes cross-write violations detectable and alertable.

- **CI enforcement**
  - Import-cycle checks + architectural tests asserting:
    - monitor wiring uses `NoopExecution`,
    - live wiring uses real execution only when armed,
    - repository writes are reachable only via application services.
