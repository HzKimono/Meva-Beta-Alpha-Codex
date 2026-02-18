# BTC Turk (btcbot) Technical Map

## Scope
This document maps runtime flow, architecture boundaries, and the self-financing subsystem using code references from `src/btcbot`.

## Entry points
- Package script: `btcbot = btcbot.cli:main`.
- Module entry: `python -m btcbot` delegates to `btcbot.cli.main()`.
- Main command families: `run` (stage3), `stage4-run`, `stage7-run`, plus doctor/backtest/replay utilities.

## Runtime initialization order
1. CLI parses subcommands and flags.
2. Settings load via pydantic-settings (`.env.live` default), with runtime secret injection/validation.
3. Logging + observability initialized.
4. Effective universe is resolved and side-effect policy is printed.
5. Command dispatcher routes to stage-specific runner.

## Stage 4 execution flow (live/dry-run compatible)
1. Acquire singleton lock and build exchange adapter (`DryRunExchangeClientStage4` or `BtcturkHttpClientStage4`).
2. Build service graph: exchange rules, accounting, lifecycle, reconciliation, risk policy/budget, execution, ledger.
3. Resolve symbols (configured or dynamic universe) and fetch mark prices/orderbook state.
4. Ingest fills into both accounting positions and normalized ledger events in one DB transaction.
5. Compute PnL report + ledger checkpoint.
6. Apply self-financing checkpoint using realized PnL delta and monotonic ledger event count.
7. Compute risk-budget mode/limits and sizing multiplier.
8. Generate intents (strategy/planning-kernel), allocate budget, map to order requests.
9. Reconcile existing orders, submit/cancel through execution service, persist run metrics and anomalies.

## Stage 7 execution flow (dry-run simulation loop)
1. Optionally run stage4 pre-cycle.
2. Load active runtime parameters + universe + exposure snapshot.
3. Compute Stage7 risk decision from drawdown/daily loss/loss streak/data age/spread-volume.
4. Build intents and lifecycle actions, then route through OMS simulator.
5. Materialize simulated fills and fee events; persist fills, idempotent ledger events, and positions.
6. Snapshot ledger financial metrics (realized/unrealized/net/equity/drawdown/turnover).
7. Persist a full `stage7_cycle` payload with decisions, intents trace, risk decision, and metrics tables.

## Data + domain boundaries
- **Adapters/Infra**: exchange HTTP/WS clients, auth signing, retry/rate-limit.
- **Domain**: immutable models (`ledger`, `risk_budget`, `strategy_core`, order/intent/risk models).
- **Application services**: cycle runners, decision pipeline, risk budget service, OMS, persistence orchestration.
- **Persistence**: SQLite `StateStore` manages schema migrations, transactions, idempotent append patterns.

## Self-financing mechanism
### Core policy
`RiskBudgetPolicy.apply_self_financing()`:
- Positive realized delta: split into `trading_capital` and `treasury` by policy ratios (default 60/40).
- Negative realized delta: subtract from trading capital only; treasury remains unchanged.

### Trigger point
`Stage4CycleRunner` invokes `RiskBudgetService.apply_self_financing_checkpoint()` each cycle after ledger ingestion and PnL report.

### Idempotency + monotonicity
- Uses `capital_policy_state.last_event_count` to ensure each ledger checkpoint is applied once.
- Duplicate checkpoint => no-op.
- Non-monotonic event count => hard error (`CapitalPolicyError`) and cycle block.

### Persistence
`capital_policy_state` table stores:
- `trading_capital_try`, `treasury_try`, `last_realized_pnl_total_try`, `last_event_count`, checkpoint metadata.

## Security posture (code-implemented)
- BTCTurk private endpoints require API key/secret; missing credentials raise configuration error.
- Request signing uses HMAC-SHA256 over `api_key + stamp`, with base64 secret decode validation.
- Nonce generator is monotonic to avoid timestamp collisions.
- Secret controls validate scope safety (`withdraw` forbidden), required scopes, and secret rotation age.
- Logging sanitizes request/response details and provides redaction helpers.

## Resilience patterns
- Retry with capped exponential backoff + jitter; honors `Retry-After` where applicable.
- Async token-bucket rate limiter for adapter layer.
- Process-level single-instance lock per runtime mode.
- SQLite transactions around critical multi-table writes.
- Stage7 and stage4 include explicit risk guardrail modes (`NORMAL`, `REDUCE_RISK_ONLY`, `OBSERVE_ONLY`).

## Known non-goals / assumptions
- No autonomous operational-expense accounting found (compute/API subscription costs are emitted as zero placeholders in capital policy decisions).
- No leverage/margin engine observed; logic appears spot-focused.

