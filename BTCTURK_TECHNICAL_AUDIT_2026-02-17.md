# BTCTurk Trading Agent Technical Audit (Repository-Based)

## 1) System blueprint (verified from code)

### Runtime paths
| Path | Entry point | Core flow |
|---|---|---|
| Stage3 | `btcbot.cli.run_cycle` | `PortfolioService -> MarketDataService -> AccountingService -> StrategyService(ProfitAwareStrategyV1) -> RiskService(RiskPolicy) -> ExecutionService` |
| Stage4 | `btcbot.cli.run_cycle_stage4` | `Stage4CycleRunner.run_one_cycle` (separate lifecycle pipeline) |
| Stage7 | `btcbot.cli.run_cycle_stage7` | `Stage7CycleRunner` + shared `PlanningKernel` |

### BTCTurk integration surface
- Synchronous adapter used by runtime exchange factory: `btcbot.adapters.btcturk_http.BtcturkHttpClient`.
- Async reliability adapters exist but are not wired into Stage3 factory/runtime:
  - `btcbot.adapters.btcturk.rest_client.BtcturkRestClient`
  - `btcbot.adapters.btcturk.ws_client.BtcturkWsClient`

### Safety model
- Live-side effects are blocked unless all gates align (`DRY_RUN=false`, `KILL_SWITCH=false`, `LIVE_TRADING=true`, `LIVE_TRADING_ACK=I_UNDERSTAND`) and validated both in config and runtime arm checks.
- `SAFE_MODE` additionally forces observe-only behavior.

## 2) Correctness / safety / security gaps

| ID | Finding | Evidence | Impact | Risk |
|---|---|---|---|---|
| G1 | WS market-data mode appears non-functional in Stage3 runtime wiring. | `MarketDataService` has WS ingestion APIs, but no call sites to `set_ws_connected` / `ingest_ws_best_bid` in `src/`; only definition exists. | If `MARKET_DATA_MODE=ws`, freshness can stay stale and block trading cycles fail-closed. | High |
| G2 | Reliability config flags may be dead/unapplied for active Stage3 exchange client. | Settings define `BTCTURK_REST_RELIABILITY_ENABLED` and retry knobs, but Stage3 factory instantiates sync `BtcturkHttpClient` directly; async `BtcturkRestClient` not used by runtime factory. | Operator expectation mismatch; tuning env vars may not affect active path. | Medium |
| G3 | Retry jitter seed is fixed (`17`) in multiple HTTP retry paths. | `jitter_seed=17` in `BtcturkHttpClient._get` and deterministic delay in async rest retry classification. | Correlated retries across processes can amplify bursts under incidents. | Medium |
| G4 | Broad exception swallowing in lifecycle refresh path can hide persistent reconciliation failures. | `ExecutionService.refresh_order_lifecycle` catches `Exception` around exchange calls and continues loop. | Silent drift between local and exchange order state; delayed detection. | Medium |
| G5 | Stage complexity overlap (Stage3/4/7) increases accidental misconfiguration surface. | Single settings model contains mixed stage flags and many mode-specific knobs. | Operational error risk; harder to reason about active controls. | Medium |

## 3) Minimal-risk refactor plan

| Refactor | Recommendation | Rationale | Risk | Scope |
|---|---|---|---|---|
| R1 | Add explicit runtime guard: reject `MARKET_DATA_MODE=ws` in Stage3 unless a WS ingest runner is attached. | Prevents false confidence and fail-closed loops without data feed. | Low | S |
| R2 | Wire one canonical BTCTurk client path for Stage3 (either adopt async `BtcturkRestClient` or remove dead flags). | Aligns config semantics with real runtime behavior. | Medium | M |
| R3 | Replace fixed retry seed with per-process randomized seed (stable per process). | Reduces thundering-herd behavior while preserving determinism within process. | Low | S |
| R4 | Upgrade reconciliation exception handling: count consecutive failures + raise/kill-switch after threshold. | Converts hidden drift into explicit safety action. | Medium | M |
| R5 | Split settings into stage-specific models (or validators gated by selected mode). | Reduces cognitive load and misconfiguration risk. | Medium | M/L |

## 4) UNKNOWNs / exact artifacts needed for complete audit

- UNKNOWN: production deployment topology (single process vs many workers).
  - Needed artifact: deployment manifests/systemd/k8s/compose files actually used in production.
- UNKNOWN: credential/secret rotation process beyond env validation fields.
  - Needed artifact: runbooks or automation scripts for key provisioning/rotation.
- UNKNOWN: exchange-side order semantics assumptions (e.g., idempotency guarantees per endpoint).
  - Needed artifact: BTCTurk API contract/spec version pinned by ops.
- UNKNOWN: end-to-end SLO/error budget and alert routing.
  - Needed artifact: monitoring/alert config (Prometheus/Grafana/Alertmanager, etc.).
