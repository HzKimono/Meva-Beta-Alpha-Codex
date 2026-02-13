# Stage 6.4 — Degrade Modes & Anomaly Detection

## Anomaly codes

- `STALE_MARKET_DATA`: market data age exceeds configured freshness threshold.
- `EXCHANGE_LATENCY_SPIKE`: cycle latency exceeds configured threshold.
- `ORDER_REJECT_SPIKE`: per-cycle rejects exceed threshold.
- `CLOCK_SKEW`: timestamp skew between cycle clock and persisted snapshot clock.
- `CURSOR_STALL`: fill cursor has stalled for configured number of cycles.
- `PNL_DIVERGENCE`: accounting PnL and ledger PnL diverge beyond warn/error thresholds.

All anomaly reason codes are stable enum values and persisted as code strings.

## Thresholds and cooldowns

Default settings:

- stale market data: 30s
- reject spike threshold: 3
- latency spike threshold: 2000ms (optional detector)
- cursor stall cycles: 5
- pnl divergence warn/error: 50 TRY / 200 TRY
- degrade warn streak/threshold: consecutive warn cycles / 3 warn cycles
- warn codes CSV: `STALE_MARKET_DATA,ORDER_REJECT_SPIKE,PNL_DIVERGENCE`

Degrade policy:

1. If cooldown is active, override cannot be upgraded during cooldown.
2. Any `ERROR` anomaly forces `OBSERVE_ONLY` and sets a 30 minute cooldown.
3. Warn threshold reached for configured warn codes forces `REDUCE_RISK_ONLY` and sets a 15 minute cooldown. The current implementation tracks a consecutive warn-cycle streak (`warn_window_count` resets to 0 when a cycle has no matching WARN anomaly), not a sliding last-N-cycles window.
4. Otherwise no degrade override is active.

## Interaction with risk budget

`risk_budget` produces a deterministic base mode each cycle. Stage 6.4 computes a degrade override and combines both with strict monotonicity:

- `final_mode = combine_modes(base_mode, override)`
- Overrides can only tighten safety:
  - `OBSERVE_ONLY` is strictest
  - `REDUCE_RISK_ONLY` is middle
  - `NORMAL` is least strict

Anomalies can never make execution more permissive than `risk_budget`.

## Persistence and observability

State store persistence:

- `anomaly_events`: per-cycle anomaly audit trail with timestamp, code, severity, and JSON details.
- `degrade_state_current`: single-row state machine persistence for current override, cooldown, and reason list.

Structured logs:

- `anomalies_detected`
- `degrade_decision`
- `final_mode`

Pre-exec gating uses last cycle rejects; post-exec persists authoritative current rejects.

## Operational playbook

1. Inspect latest `anomaly_events` rows for code frequency and details.
2. Check `degrade_state_current` for active cooldown and current override.
3. Validate if anomalies are transient (market data lag, temporary rejects) or systemic (clock skew, pnl divergence).
4. If cooldown is active, wait for expiry; mode upgrades are intentionally blocked.
5. Resolve root cause and verify anomalies clear in subsequent cycles before expecting `NORMAL` mode.
