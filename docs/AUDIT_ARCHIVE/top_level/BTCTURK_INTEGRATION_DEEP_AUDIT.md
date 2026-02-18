# BTCTURK INTEGRATION DEEP AUDIT

## A) API surface map: endpoints, request/response handling, auth, signing, timestamping

### A.1 Synchronous adapter (`src/btcbot/adapters/btcturk_http.py`)

| Endpoint | Method | Caller function(s) | Auth | Request handling | Response handling |
|---|---|---|---|---|---|
| `/api/v2/server/exchangeinfo` | GET | `BtcturkHttpClient.get_exchange_info`, `health_check` | Public | via `_get(path)` | `_extract_list_data` -> `_to_pair_info` with row-level skip on malformed rows; raises if all rows malformed. |
| `/api/v2/orderbook` | GET | `BtcturkHttpClient.get_orderbook` | Public | `pairSymbol`, optional `limit` | requires `data` dict; parses best prices via `_parse_best_price`; hard-fails on malformed depth/price. |
| `/api/v2/ticker` | GET | `BtcturkHttpClient.get_ticker_stats` | Public | none | `_extract_list_data`. |
| `/api/v1/users/balances` | GET | `BtcturkHttpClient.get_balances` | Private | `_private_get` | validates list shape; maps with `_to_balance_item` to `Balance`. |
| `/api/v1/openOrders` | GET | `BtcturkHttpClient.get_open_orders` | Private | requires `pairSymbol` | requires `data.bids`/`data.asks` lists; each item parsed by `_to_open_order_item`; fails if any malformed item. |
| `/api/v1/allOrders` | GET | `BtcturkHttpClient.get_all_orders` | Private | `pairSymbol`, `startDate`, `endDate` | `_extract_order_rows` -> `_to_order_snapshot`. |
| `/api/v1/order/{order_id}` | GET | `BtcturkHttpClient.get_order` | Private | path param | parses first row from `_extract_order_rows`. |
| `/api/v1/order` | POST | `_submit_limit_order_legacy`, `submit_limit_order` | Private | `_build_submit_order_payload` (`pairSymbol`,`price`,`quantity`,`orderMethod=limit`,`orderType`,`newOrderClientId`) | requires `data.id`; otherwise `ExchangeError`. |
| `/api/v1/order` | DELETE | `cancel_order`, `cancel_order_by_client_order_id` | Private | payload either `{id:int}` or `{orderClientId:str}` | returns `success==True` bool; no deep semantic validation. |
| `/api/v1/users/transactions/trade` | GET | `get_recent_fills` | Private | `pairSymbol`, optional `startDate` | `_extract_fill_rows` flexible list extraction; maps into `TradeFill` with fallback id fields. |

### A.2 Async adapter (`src/btcbot/adapters/btcturk/rest_client.py`)
- Generic request method: `BtcturkRestClient.request(method, path, is_private, params, json_body, correlation_id)`.
- Used explicit order-safe wrappers:
  - `submit_order_safe` -> `POST /api/v1/order`, then optional existence probe by `client_order_id`.
  - `cancel_order_safe` -> `DELETE /api/v1/order/{order_id}`, then optional open-order probe.
  - `find_open_order_by_client_order_id` and `is_order_open` both query `GET /api/v1/openOrders`.

### A.3 Auth, signing, timestamping

**Sync path**
- Auth headers produced by `build_auth_headers` (`src/btcbot/adapters/btcturk_auth.py`).
- Signature formula: `base64(hmac_sha256(base64_decode(api_secret), f"{api_key}{stamp_ms}"))` in `compute_signature`.
- Timestamp source: `MonotonicNonceGenerator.next_stamp_ms` ensures strictly increasing stamp locally.
- Applied in `BtcturkHttpClient._private_request` as headers `X-PCK`, `X-Stamp`, `X-Signature`.

**Async path**
- Auth built in `BtcturkRestClient._auth_headers`.
- Timestamp source: `ClockSyncService.stamped_now_ms` (offset-adjusted local clock).
- Clock sync logic in `ClockSyncService.sync/maybe_sync`; offset clamped to `max_abs_offset_ms`.

### A.4 Response/error normalization
- Sync private errors: `_private_request` converts non-200 or unsuccessful payload into rich `ExchangeError` (status, code, message, sanitized request fields).
- Sync public errors: `_get` + `retry_with_backoff`; rejects non-dict payload and `success=false` payloads.
- Async errors: `_raise_http_error` -> `RestRequestError` (`NETWORK/RATE_LIMIT/SERVER/CLIENT/EXCHANGE`) then `_to_exchange_error` after retry classification.

---

## B) Rate-limit strategy: backoff, retries, jitter, max attempts, timeout policy

### B.1 Sync (`BtcturkHttpClient`)
- Public GET retries are delegated to `retry_with_backoff` with:
  - `max_attempts=4` (`_RETRY_ATTEMPTS`),
  - base delay 400ms, max delay 4000ms,
  - jitter enabled (`jitter_seed=17`),
  - retry on `TimeoutException`, `TransportError`, `HTTPStatusError`,
  - optional `Retry-After` override via `parse_retry_after_seconds`.
- Private GET retries use custom loop in `_private_get`:
  - same attempt count (4),
  - retries only on status {429, 500, 502, 503, 504},
  - total wait capped by `_RETRY_TOTAL_WAIT_CAP_SECONDS=8s`.

### B.2 Async (`BtcturkRestClient`)
- Pre-request rate limiter: `AsyncTokenBucket.acquire` (rps + burst), guarded by `asyncio.Lock`.
- Retry logic uses `async_retry` + `compute_delay`:
  - retryable kinds: `NETWORK`, `SERVER`, `RATE_LIMIT`,
  - delay strategy: exponential + jitter + `Retry-After` override,
  - attempts controlled by `RestReliabilityConfig.max_attempts` (default 4).
- Timeout policy:
  - connect/read/write/pool each explicitly configured in `RestReliabilityConfig` (default 5/10/10/5 sec).

### B.3 Websocket reconnect/backoff
- `BtcturkWsClient.run` reconnect loop:
  - increments reconnect counters,
  - backoff `base_backoff_seconds * 2^(attempt-1)` capped by `max_backoff_seconds`,
  - jitter multiplier `[0.8,1.2)`.
- Idle timeout handling in `_read_loop` via `asyncio.wait_for(socket.recv(), timeout=idle_reconnect_seconds)`.

---

## C) Order lifecycle correctness (partial fills, cancel/replace, rejections, expired orders)

### C.1 Placement and ACK
- Stage3 order placement path: `ExecutionService.execute_intents` -> `exchange.place_limit_order`.
- BTCTurk sync placement: `BtcturkHttpClient.place_limit_order` -> `_submit_limit_order_legacy`.
- Stage4 adapter placement: `BtcturkHttpClient.submit_limit_order` with explicit rule validation + quantization.

### C.2 Partial fills and fills ingestion
- Partial/open/filled statuses are interpreted from exchange raw status via `_parse_exchange_status` mapping.
- Open order snapshots from `/openOrders` and `/allOrders` are transformed by `_to_open_order_item` and `_to_order_snapshot`.
- Fill ingestion for accounting uses `/users/transactions/trade` through `get_recent_fills`, then `AccountingService.refresh` applies fills idempotently using `StateStore.save_fill`.

### C.3 Cancel and stale-expiry semantics
- Stale cancellation logic is in `ExecutionService.cancel_stale_orders` using age > `ttl_seconds`.
- Cancel API calls:
  - by order id: `cancel_order` (`DELETE /api/v1/order` payload id),
  - by client id: `cancel_order_by_client_order_id`.
- Uncertain cancel errors trigger `_reconcile_cancel` (checks open orders + all orders) and transition to `CANCELED`, `FILLED`, or `UNKNOWN`.

### C.4 Replace behavior
- Native amend/replace endpoint is not implemented.
- Practical behavior is cancel + submit path, controlled by caller logic; explicit atomic replace semantics are **UNKNOWN** (no dedicated replace primitive in current adapter).

### C.5 Rejections
- Rejections are surfaced by:
  - HTTP non-200 and payload `success=false` in `_private_request`.
  - Status parsing includes `rejected` in `_parse_exchange_status`.
- Execution layer logs non-uncertain `ExchangeError` and does not auto-resubmit blindly.

---

## D) Idempotency & duplication prevention (clientOrderId, dedupe keys, replay protection)

### D.1 Client order ID usage
- Stage3 live submit requires `client_order_id` in `ExecutionService.execute_intents` via `make_client_order_id(intent)`.
- BTCTurk payload key is `newOrderClientId` (`_build_submit_order_payload`).
- Stage4/Stage7 helper: deterministic BTCTurk-safe ID builder in `client_order_id_service.build_exchange_client_id`.

### D.2 DB dedupe keys (local replay protection)
- `ExecutionService` records `state_store.record_action(cycle_id, action_type, payload_hash)` before side effects.
- Duplicate action rows are skipped (`action_id is None`), preventing repeated submit/cancel from same dedupe key.
- `StateStore` maintains unique index for action `dedupe_key` and idempotency-aware intent/order records.

### D.3 Safe submit/cancel wrappers in async client
- `submit_order_safe` checks existing open order by `client_order_id` on retryable failures.
- `cancel_order_safe` treats some 400/404/409 responses as idempotent success when order is no longer open.

### D.4 Replay protection status
- Local replay protection exists (DB-level dedupe + idempotency keys).
- Exchange-side replay protection relies on `newOrderClientId`; strict uniqueness horizon on BTC Turk side is **UNKNOWN** without external API contract evidence.

---

## E) Reconciliation: mismatch detection and repair

### E.1 Stage3 reconciliation
- `ExecutionService.refresh_order_lifecycle`:
  1. loads local open/unknown orders from `StateStore.find_open_or_unknown_orders`,
  2. fetches exchange open orders (`get_open_orders`) and marks known-open,
  3. for missing locals, fetches `get_all_orders` window and tries `_match_existing_order`,
  4. updates local status via `StateStore.update_order_status`.
- On submit uncertainty (`_reconcile_submit`): probes openOrders/allOrders by client id and fallback field matching.
- On cancel uncertainty (`_reconcile_cancel`): determines if still open, filled, canceled, or unknown.

### E.2 Startup reconciliation
- `StartupRecoveryService.run` executes at cycle startup:
  - invokes `refresh_order_lifecycle`,
  - refreshes fills/accounting,
  - checks invariants (no negative balances/position qty),
  - can force observe-only behavior by setting execution kill switch.

### E.3 Stage4 open-order reconciliation
- `ReconcileService.resolve` compares exchange open orders vs DB open orders keyed by `client_order_id`:
  - marks DB-only as unknown closed,
  - imports exchange-only as `mode="external"`,
  - enriches missing exchange IDs.

### E.4 WS + REST merge reconciler
- `btcturk/reconcile.py::Reconciler.merge` merges:
  - REST open-order truth,
  - WS fill events,
  - WS terminal updates,
  - dedupes by `fill_id`, aggregates by order key, and strips terminal CANCELED/FILLED from open set.

---

## F) Failure modes table (expected behavior + recovery actions)

| Failure mode | Detection location | Expected behavior in code | Recovery action |
|---|---|---|---|
| Network flap / timeout (REST) | `BtcturkHttpClient._get`, `_private_get`; `BtcturkRestClient.request` | Retry with backoff/jitter up to max attempts; may propagate `ExchangeError`. | Keep dry-run/safe-mode if recurring; rely on reconciliation before next cycle. |
| 5xx responses | same as above | Classified retryable; async path marks `SERVER`, sync retries configured statuses. | Exponential retry; if exhausted, execution logs and skips intent. |
| 429 rate-limit | sync `_private_get`; async `_raise_http_error` + retry classifier | increments 429 metrics; honors `Retry-After` when present. | Lower RPS/burst configs, increase delays, fallback observe-only if persistent. |
| Stale market data | Stage3 uses non-positive bids count; Stage7 market freshness checks | can increase stale counters; guardrails/risk may force observe-only path. | Pause live writes; refresh feed and clock sync. |
| Clock skew | `ClockSyncService.sync/maybe_sync` | offset corrected and clamped to max abs offset. | periodic sync + anomaly alert (TODO present in code comments). |
| Websocket drop | `BtcturkWsClient.run` exception path | increments drop/reconnect metrics; reconnect with jittered backoff. | auto reconnect; operator alert if reconnect storm. |
| Websocket idle/no messages | `BtcturkWsClient._read_loop` timeout -> `WsIdleTimeoutError` | treated as disconnect, triggers reconnect loop. | reconnect and monitor freshness counters. |
| WS sequence gaps / malformed payload | WS parser / handlers | invalid envelopes dropped + metric increments; no strict sequence repair logic. | rely on REST reconciliation/openOrders polling. |
| Uncertain submit outcome | `ExecutionService.execute_intents` exception branch | `_reconcile_submit` attempts to confirm existence; can persist UNKNOWN. | follow-up lifecycle refresh and allOrders matching. |
| Uncertain cancel outcome | `ExecutionService.cancel_stale_orders` exception branch | `_reconcile_cancel` classifies filled/canceled/unknown. | lifecycle refresh on future cycles; manual review if unknown persists. |

---

## Possible bug vectors (security/reliability/financial) tied to code

1. **Auth secret decoding mismatch risk**: `compute_signature` validates base64 strictly, but async `_auth_headers` uses `base64.b64decode` without `validate=True`; malformed secret behavior could diverge across clients (`btcturk_auth.py::compute_signature` vs `btcturk/rest_client.py::_auth_headers`).
2. **Cancel endpoint inconsistency**: async `cancel_order_safe` calls `DELETE /api/v1/order/{order_id}` while sync path uses `DELETE /api/v1/order` with JSON body; one path may be incompatible if exchange supports only one style (`rest_client.py::cancel_order_safe` vs `btcturk_http.py::cancel_order`).
3. **Status-mapping blind spots**: `_parse_exchange_status` only maps specific strings; new/variant statuses fall to `UNKNOWN` and may degrade lifecycle correctness (`btcturk_http.py::_parse_exchange_status`).
4. **Fill identity collision risk**: `get_recent_fills` fallback `fill_id` chooses `id` or `orderClientId` or `orderId`; if upstream lacks unique fill ids, dedupe may collapse distinct partial fills (`btcturk_http.py::get_recent_fills`).
5. **All-orders time-window miss**: reconciliation uses bounded windows (e.g., last hour/five minutes in execution paths); delayed exchange visibility can cause false `NOT_FOUND/UNKNOWN` (`execution_service.py::_reconcile_submit`, `_reconcile_cancel`).
6. **Public/Private retry policy split**: sync `_private_request` itself has no retry wrapper; only `_private_get` retries. POST/DELETE depend on upper layers and may fail noisily under transient network glitches (`btcturk_http.py::_private_request`, `_private_get`).
7. **Fallback symbol rules may be unsafe**: if exchange info fetch fails, `_resolve_symbol_rules` fabricates conservative defaults that may still diverge from actual exchange constraints and cause submit rejects or mis-quantization (`btcturk_http.py::_resolve_symbol_rules`).
8. **Queue overflow data loss**: websocket backpressure drops messages (`ws_backpressure_drops`) without replay gap fill in the same module, potentially missing fills/terminal updates until REST reconcile catches up (`btcturk/ws_client.py::_read_loop`).
9. **Clock offset clamping hides severe skew**: `ClockSyncService.sync` silently clamps large offset rather than hard-failing; signatures may still be rejected near boundary conditions (`btcturk/clock_sync.py::sync`).
10. **Open-orders parser strictness**: `get_open_orders` fails entire call if any one row malformed (length mismatch check), causing full-cycle degradation from a single bad item (`btcturk_http.py::get_open_orders`).
11. **Potential duplicate submit on uncertain post-send failure**: sync submit path in execution relies on reconcile heuristics; if reconciliation misses and retry happens upstream, duplicate live orders are possible (`execution_service.py::execute_intents`, `_reconcile_submit`).
12. **Client-order-id optionality across paths**: some methods accept nullable `client_order_id`, others require it; inconsistency can weaken idempotent matching and audit linkage (`btcturk_http.py::place_limit_order`, `submit_limit_order`, and execution metadata attachment paths).

---

## Evidence coverage notes
- This audit is code-driven from: `src/btcbot/adapters/btcturk_http.py`, `src/btcbot/adapters/btcturk_auth.py`, `src/btcbot/adapters/btcturk/rest_client.py`, `src/btcbot/adapters/btcturk/retry.py`, `src/btcbot/adapters/btcturk/rate_limit.py`, `src/btcbot/adapters/btcturk/clock_sync.py`, `src/btcbot/adapters/btcturk/reconcile.py`, `src/btcbot/adapters/btcturk/ws_client.py`, plus lifecycle callers in `src/btcbot/services/execution_service.py`, `src/btcbot/services/startup_recovery.py`, `src/btcbot/services/reconcile_service.py`, and `src/btcbot/services/client_order_id_service.py`.
