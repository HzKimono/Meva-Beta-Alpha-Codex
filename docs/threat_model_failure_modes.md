# Threat Model & Failure Modes: Crypto Agent Bot (Python Source-of-Truth)

## Scope and assumptions
- Scope aligns to `agent/`, `data/`, `signals/`, `risk/`, `execution/`, `portfolio/`, and `ops/` module boundaries.
- Production mode includes live exchange API usage with signing keys and continuous loop operation.
- Prioritization uses **Severity** (Critical/High/Medium/Low) and **Likelihood** (High/Medium/Low).

---

## 1) Threat model (assets, adversaries, attack surfaces)

### 1.1 Critical assets
1. **Exchange credentials**: API key, secret, passphrase, signing material.
2. **Signing integrity**: canonical request payload, nonce/timestamp, signature routine.
3. **Order authority**: ability to place/cancel orders.
4. **Canonical state integrity**: balances, positions, open orders, risk limits, runtime flags.
5. **Audit and ledger integrity**: immutable decision records and PnL/cost ledgers.
6. **Ops control plane**: pause/resume, risk limit changes, kill-switch toggles.
7. **Dependency supply chain trust**: pinned package hashes, container image provenance.
8. **Logs/metrics channels**: may leak secrets or facilitate manipulation if unauthenticated.
9. **Webhook/alert endpoints**: could be abused for alert spoofing or command injection.

### 1.2 Adversaries
1. **External attacker** targeting exposed interfaces (webhooks, control API, CI/CD, artifact registry).
2. **Credential thief** via env leak, logs, memory scrape, misconfigured secret store.
3. **Malicious/compromised dependency maintainer** (supply-chain insertion).
4. **Exchange-side uncertainty** (outages, stale responses, inconsistent order states).
5. **Insider/operator error** (unsafe override, wrong config promotion, accidental key exposure).

### 1.3 Attack surfaces mapped to Python modules
| Surface | Example attack | Primary modules | Severity | Likelihood |
|---|---|---|---|---|
| API key storage/injection | Secret leakage via env dump/logging | `ops/`, `agent/`, `execution/` | Critical | Medium |
| Request signing path | Nonce replay/timestamp drift causing rejected or replayable requests | `execution/` | High | Medium |
| Exchange adapter transport | MITM/downgrade or endpoint misconfiguration | `execution/`, `data/` | High | Low-Med |
| Webhook/control endpoints | Forged commands or alert spoofing | `ops/` | Critical | Medium |
| Structured logs | Secret or PII leakage; log forging | `ops/`, cross-module | High | Medium |
| Dependency resolution/build | Typosquat package / compromised transitive dependency | build/`scripts/` | Critical | Medium |
| State store persistence | Corruption/tampering of positions/orders/limits | `portfolio/`, `agent/` | Critical | Low-Med |
| Config promotion | Unsafe risk limits in prod | `config/`, `agent/`, `risk/` | Critical | Medium |
| Time source | Clock skew breaks signatures and sequence validation | `execution/`, `data/`, `agent/` | High | Medium |

### 1.4 Priority threat register (top actionable items)
| ID | Threat | Impact | Sev | Likelihood | Priority |
|---|---|---|---|---|---|
| T1 | API key compromise | Unauthorized trading and potential account depletion | Critical | Medium | P0 |
| T2 | Risk policy bypass (bug/override misuse) | Catastrophic loss via unbounded exposure | Critical | Medium | P0 |
| T3 | Execution uncertainty (timeout + duplicate submit) | Duplicate/phantom orders; loss amplification | Critical | Medium | P0 |
| T4 | State/accounting divergence | Incorrect PnL, bad risk decisions, false self-financing signals | High | Medium | P1 |
| T5 | Supply-chain compromise | Arbitrary code execution in bot runtime | Critical | Medium | P0 |
| T6 | Control-plane abuse | Kill-switch disable or risk-limit inflation | Critical | Low-Med | P1 |
| T7 | Data feed poisoning/staleness | Wrong signals/execution in abnormal market states | High | High | P0 |

---

## 2) Failure modes by subsystem

## 2.1 `data/` (feed ingestion, normalization, freshness)
| Failure mode | Observable symptom | Sev | Likelihood | Detection | Immediate action |
|---|---|---|---|---|---|
| WS disconnect loop | rising reconnects, stale snapshots | High | High | `ws_reconnect_count`, `md_staleness_s` | Halt new entries; switch to REST backfill-only mode |
| Sequence gaps/out-of-order events | feature discontinuity, invalid bars | High | Medium | sequence checks, gap counters | Mark symbol unhealthy; block symbol trading |
| Exchange stale market data | stale best bid/ask beyond threshold | High | Medium | freshness gate | Reject signals for affected symbols |
| Normalizer schema drift | parse errors after exchange API change | Medium-High | Medium | parse error rate alert | Fail closed for unknown payload fields; escalate |

## 2.2 `signals/` (strategy generation)
| Failure mode | Observable symptom | Sev | Likelihood | Detection | Immediate action |
|---|---|---|---|---|---|
| Feature NaN/invalid values | bursts of abnormal signals | High | Medium | validation rejects, anomaly counters | Suppress strategy and fallback to no-trade |
| Strategy logic regression | sudden directional bias / turnover spike | High | Medium | replay parity checks, drift monitors | Disable strategy via runtime flag |
| Expired signal execution | delayed intents still submitted | Medium | Medium | signal-age check | Reject stale intents pre-risk gate |

## 2.3 `risk/` (limits and safety)
| Failure mode | Observable symptom | Sev | Likelihood | Detection | Immediate action |
|---|---|---|---|---|---|
| Daily loss limit not enforced | continued trading after breach | Critical | Low-Med | invariant checks per tick | Activate kill-switch; open incident |
| Exposure calc bug | post-trade exposure > cap | Critical | Medium | post-trade shadow calc | Hard reject + reconcile state |
| Kill-switch not propagating | orders still submitted when active | Critical | Low | control-action audit mismatch | Block executor path globally |

## 2.4 `execution/` (order lifecycle, retries, reconcile)
| Failure mode | Observable symptom | Sev | Likelihood | Detection | Immediate action |
|---|---|---|---|---|---|
| Timeout on submit with unknown status | uncertain order state | Critical | Medium | submit timeout + no ack | Freeze new entries, run reconcile loop |
| Idempotency key collision/misuse | duplicate or missing orders | Critical | Medium | idempotency conflict metrics | reject duplicate payload mismatch |
| Cancel failures under volatility | stuck open orders | Critical | Medium | cancel retry counters | escalate to kill-switch + reduce risk budget |
| Exchange partial outages | random 5xx/reject spikes | High | High | error rate, latency alerts | circuit breaker open; throttle down |

## 2.5 `portfolio/` (PnL and ledger)
| Failure mode | Observable symptom | Sev | Likelihood | Detection | Immediate action |
|---|---|---|---|---|---|
| Fill accounting mismatch | realized PnL drift vs exchange | High | Medium | periodic reconciliation diffs | freeze treasury transfers, investigate |
| Fee under-accounting | inflated surplus/self-financing false positive | High | Medium | fee sanity checks | apply conservative fee fallback |
| Principal baseline mutation | false principal protection status | Critical | Low | immutable baseline checksum | lock config + incident |

## 2.6 `ops/` and `agent/` (control, observability, orchestration)
| Failure mode | Observable symptom | Sev | Likelihood | Detection | Immediate action |
|---|---|---|---|---|---|
| Missing heartbeat/tick stalls | no decisions/audits for interval | High | Medium | heartbeat lag alert | restart orchestrator in safe mode |
| Alerting pipeline down | silent critical failures | High | Medium | dead-man switch alerts | secondary channel failover |
| Unauthorized control action | unexpected risk/kill-switch change | Critical | Low-Med | signed audit trail mismatch | revoke sessions; lock controls |

---

## 3) Mitigations (technical + operational)

## 3.1 Technical controls mapped to modules
| Threat/Failure | Control | Module owner | Implementation intent |
|---|---|---|---|
| T1 key compromise | Secret manager injection, short-lived creds where possible, no-withdrawal API scope, automatic redaction filters | `ops/`, `agent/`, `execution/` | Startup fails if secrets missing or broad scopes detected |
| T2 risk bypass | Risk gate as mandatory precondition in `agent` pipeline; executor refuses unsigned/ungated intents | `risk/`, `agent/`, `execution/` | No `VALIDATE pass` artifact => hard fail execute |
| T3 duplicate/uncertain orders | Deterministic `order_client_id`, idempotency store, reconcile-before-resubmit policy | `execution/` | Unknown submit state transitions to `VERIFY_REQUIRED` |
| T4 state divergence | Periodic and restart reconciliation with exchange truth; atomic state snapshots | `portfolio/`, `execution/`, `agent/` | Freeze new entries if drift unresolved > N cycles |
| T5 supply chain | Hash-pinned dependencies, signed artifacts, SBOM, CI vulnerability gate | build/`scripts/` | Block deploy on critical CVEs without exception ticket |
| T7 stale/poisoned data | freshness/sequence gates, symbol health scoring, fail-closed signal suppression | `data/`, `signals/` | unhealthy symbol -> no new intents |
| Time drift | NTP sync guard + monotonic clock checks for nonce/timestamp | `agent/`, `execution/`, `data/` | If skew > threshold, disable live submit |
| Control-plane abuse | AuthN/AuthZ + MFA for controls, signed requests, replay protection | `ops/` | privileged actions require strong actor identity |
| Log tampering/leakage | append-only sink, immutable retention, sensitive-field scrubbers | `ops/` | reject log events containing secret patterns |
| API instability | adaptive rate limiter + bounded exponential backoff + circuit breaker | `execution/`, `data/` | open breaker halts entries, keeps reconcile/cancel path |

## 3.2 Operational controls (runbooks + governance)
1. **On-call severity mapping**: P0 (critical controls breached), P1 (material risk degradation), P2 (degraded but bounded).
2. **Two-person rule** for live-mode enable, hard risk-limit changes, and kill-switch deactivation.
3. **Change windows** for config promotions (`stage -> prod`) with rollback checkpoint.
4. **Daily control review**: audit all `CONTROL` actions and unresolved drift incidents.
5. **Weekly replay drills**: run deterministic replay of worst incidents and compare outputs.
6. **Quarterly key rotation drill** including emergency credential revoke workflow.

---

## 4) Incident response playbook

## 4.1 Scenario: Bad fills (unexpected slippage/price quality)
- **Trigger**: `slippage_bps` exceeds threshold for K trades or one extreme outlier.
- **Immediate (0-5 min)**:
  1. Set risk mode to defensive sizing.
  2. Halt entries for impacted symbols.
  3. Preserve all fill/order artifacts and market snapshots.
- **Stabilize (5-30 min)**:
  1. Compare expected vs realized fill path in `execution/` and `data/`.
  2. Validate market sanity gate thresholds.
- **Recovery**:
  1. Re-enable symbols only after two consecutive healthy windows.
  2. Document root cause and threshold changes.

## 4.2 Scenario: Stuck orders (cannot cancel / uncertain status)
- **Trigger**: cancel retries exceed threshold OR order remains open beyond SLA.
- **Immediate**:
  1. Activate kill-switch if exposure is increasing.
  2. Freeze new entries globally.
  3. Run high-frequency reconcile loop.
- **Stabilize**:
  1. Attempt cancel-by-client-id and cancel-by-exchange-id paths.
  2. Hedge/flatten only after confirmed state.
- **Recovery**:
  1. Resume only when all uncertain orders resolved.
  2. Add postmortem action for idempotency/retry policy.

## 4.3 Scenario: API outage (exchange or network)
- **Trigger**: sustained 5xx/timeouts over threshold window.
- **Immediate**:
  1. Open circuit breaker for new entries.
  2. Keep heartbeat and status polling for recovery.
  3. Alert P0 if open risk cannot be managed.
- **Stabilize**:
  1. Degrade to read-only/reconcile mode.
  2. Monitor exchange status + regional connectivity.
- **Recovery**:
  1. Half-open breaker with canary symbol and minimal size.
  2. Return to normal only after success-rate and latency normalize.

## 4.4 Scenario: PnL anomaly (unexpected ledger divergence)
- **Trigger**: drift between internal and exchange/account statements above tolerance.
- **Immediate**:
  1. Freeze treasury transfers and risk scaling updates.
  2. Halt new entries if anomaly is material.
  3. Snapshot state store and ledger versions.
- **Stabilize**:
  1. Recompute PnL from fills source-of-truth.
  2. Validate fee model and FX conversion assumptions.
- **Recovery**:
  1. Backfill corrected ledger entries with audit references.
  2. Resume only after reconciliation sign-off.

## 4.5 Scenario: Key compromise
- **Trigger**: suspected secret leak, unauthorized orders, anomalous API usage.
- **Immediate (containment)**:
  1. Activate kill-switch.
  2. Revoke compromised keys at exchange.
  3. Disable control-plane sessions/tokens potentially linked.
- **Eradication**:
  1. Rotate all related secrets and signing credentials.
  2. Rebuild runtime from trusted image and clean config.
- **Recovery**:
  1. Re-enable with fresh least-privilege keys.
  2. Conduct mandatory incident review and control hardening.

---

## 5) Minimal security baseline checklist (production)

### Identity, secrets, and access
- [ ] Exchange API keys are trade-only (no withdrawals) and scoped minimally.
- [ ] Secrets are injected from secret manager/runtime env, never committed or logged.
- [ ] Operator control actions require strong auth and role-based authorization.
- [ ] High-risk actions (live mode, risk-limit changes, kill-switch disable) require two-person approval.

### Runtime and execution safety
- [ ] Kill-switch path tested end-to-end and repeat-invocation safe.
- [ ] Daily loss, drawdown, exposure, and leverage limits enforced as hard gates.
- [ ] Idempotent submission invariant verified (no duplicate effective orders).
- [ ] Reconcile-before-resubmit enforced for unknown execution outcomes.
- [ ] Circuit breaker tested for API instability and data staleness conditions.

### Integrity and observability
- [ ] Every loop step emits immutable audit records (`PLAN..RECORD`).
- [ ] Logs are structured, redacted, and retained in append-only sink.
- [ ] Critical alerts configured: kill-switch, loss breach, drift unresolved, heartbeat missing.
- [ ] Dead-man-switch alert path validated (alerting pipeline health).

### Supply chain and deployment hygiene
- [ ] Dependencies are pinned with hashes; critical CVEs block deploy.
- [ ] Container images are signed/scanned; provenance is verifiable.
- [ ] Config promotion path (`dev->stage->prod`) has approvals and rollback artifacts.
- [ ] Time sync guardrails (NTP/skew detection) enforced for signing and sequencing.

### Incident readiness
- [ ] Runbooks exist and are current for bad fills, stuck orders, API outage, PnL anomaly, key compromise.
- [ ] Quarterly incident drill and key-rotation exercise completed and evidenced.
- [ ] Postmortem template includes control-gap tracking and owner due dates.

---

## Priority implementation order (recommended)
1. **P0**: key protection, kill-switch hard gate, idempotent execution + reconcile-before-resubmit, data freshness fail-closed, dependency pinning/CVE gate.
2. **P1**: control-plane hardening, immutable audit sink, drift auto-halt, dead-man-switch alerting.
3. **P2**: advanced anomaly detection and extended simulation drills.
