Auth & Signing Review

- Primary live adapter in runtime path: `BtcturkHttpClient` (`src/btcbot/adapters/btcturk_http.py`).
- Private auth header construction:
  - Uses `X-PCK`, `X-Stamp`, `X-Signature` via `build_auth_headers()`.
  - Signature = `base64(hmac_sha256(base64_decode(api_secret), f"{api_key}{stamp_ms}"))`.
  - Secret must be valid base64, otherwise `ValueError`.
- Timestamp/nonce handling:
  - Uses `MonotonicNonceGenerator.next_stamp_ms()` to enforce strictly increasing ms stamp in-process.
  - This reduces same-process nonce regressions.
- Runtime secondary async adapter (`src/btcbot/adapters/btcturk/rest_client.py`):
  - Calls `clock_sync.maybe_sync()` before requests.
  - Private auth recomputed per request with `stamped_now_ms()`.
- Endpoint usage observed in adapters:
  - Public: `/api/v2/server/exchangeinfo`, `/api/v2/orderbook`.
  - Private: `/api/v1/order` (POST/DELETE), `/api/v1/openOrders`, `/api/v1/allOrders`, `/api/v1/order/{id}`, `/api/v1/users/balances`, `/api/v1/users/transactions/trade`.
- Security observations:
  - Request/response data is sanitized in error paths (`sanitize_text`, `sanitize_mapping`).
  - Missing artifact for final closure: UNKNOWN whether exchange nonce/clock drift tolerance is externally monitored in production.

Rate Limit & Retry Review

- Sync runtime adapter (`btcturk_http.py`):
  - Public GET retries via `retry_with_backoff` for timeout/transport/http status failures.
  - Retry candidates include network, 429, and >=500.
  - Private GET retries in explicit loop for status `{429,500,502,503,504}` with total wait cap.
  - Backoff base 0.4s, max 4.0s, attempts 4.
  - Uses `Retry-After` when present for 429.
- Async adapter (`btcturk/rest_client.py`):
  - Explicit async token bucket limiter (`AsyncTokenBucket`) before each call.
  - Error classification:
    - `NETWORK` (timeout/transport), `RATE_LIMIT` (429), `SERVER` (>=500), `CLIENT` (4xx), `EXCHANGE` (payload-level failures).
  - Retry only for `NETWORK|SERVER|RATE_LIMIT`, with bounded backoff.
- Observed risks:
  - Fixed jitter seed (`17`) appears in retry paths -> correlated retry waves across replicas possible.
  - Active Stage3 runtime path uses sync client; async limiter/reliability controls may not govern the default run path.

Order Lifecycle Coverage Matrix (table: feature vs implemented yes/no, where)

| Feature | Implemented (Yes/No) | Where |
|---|---|---|
| Submit limit order | Yes | `BtcturkHttpClient.place_limit_order`, `submit_limit_order`, `ExecutionService.execute_intents` |
| Cancel by exchange order id | Yes | `BtcturkHttpClient.cancel_order`, `cancel_order_by_exchange_id`; `ExecutionService.cancel_stale_orders` |
| Cancel by client order id | Yes | `BtcturkHttpClient.cancel_order_by_client_order_id` |
| Query open orders | Yes | `BtcturkHttpClient.get_open_orders`, `list_open_orders`; `ExecutionService.refresh_order_lifecycle` |
| Query historical/all orders for reconciliation | Yes | `BtcturkHttpClient.get_all_orders`; used in `ExecutionService.refresh_order_lifecycle` and stale pending recovery |
| Fills fetch/reconciliation | Yes | `BtcturkHttpClient.get_recent_fills`; accounting refresh + Stage4 reconcile/fills flow |
| Partial fill status handling | Yes | Exchange status mapping to `OrderStatus.PARTIAL` in adapter/listing paths |
| Uncertain submit reconciliation | Yes | `ExecutionService._reconcile_submit` path, fallback to open/all orders and UNKNOWN state persistence |
| Idempotent submit dedupe | Yes (local-store scope) | `StateStore.idempotency_keys` + `ExecutionService.reserve/finalize` flow |
| Cancel/replace (atomic amend) | No | No explicit single API flow; cancel + new submit exists as separate operations |
| Exactly-once exchange-side guarantee | No (not provable from repo) | Compensating controls exist (idempotency keys + client_order_id reconciliation), but transport retries remain at-least-once |

Critical Bugs/Risks (ranked)

1. High — WS integration gap in active Stage3 loop
- Risk: WS client exists, but default runtime path is timer+REST oriented; WS market-data production wiring is not clearly active in Stage3 execution path.
- Impact: If operators expect WS semantics, they may still run on REST/fallback behavior; stale gating may block cycles.
- Files: `src/btcbot/cli.py`, `src/btcbot/services/market_data_service.py`, `src/btcbot/adapters/btcturk/ws_client.py`.

2. High — Retry burst correlation
- Risk: Deterministic/fixed jitter seed can synchronize retries across multiple bot instances during incident windows.
- Impact: Amplified throttling/429 storms and slower recovery.
- Files: `src/btcbot/adapters/btcturk_http.py`, `src/btcbot/adapters/btcturk/rest_client.py`.

3. Medium — Reconciliation failure can degrade silently over long windows
- Risk: `refresh_order_lifecycle` catches broad exceptions and continues.
- Impact: Persistent divergence between local state and exchange may not trigger hard escalation quickly.
- Files: `src/btcbot/services/execution_service.py`.

4. Medium — Dual adapter reliability semantics (sync active vs async richer controls)
- Risk: Default runtime uses sync adapter while async adapter has stronger explicit limiter/error model.
- Impact: Operator assumptions about configured rate-limit/reliability knobs can diverge from active path.
- Files: `src/btcbot/services/exchange_factory.py`, `src/btcbot/adapters/btcturk_http.py`, `src/btcbot/adapters/btcturk/rest_client.py`.

5. Medium — Replace order lifecycle not explicit
- Risk: No atomic cancel/replace capability in integration layer.
- Impact: Slippage/queue-position risk during two-step modify flows.
- Files: `src/btcbot/adapters/btcturk_http.py`, `src/btcbot/services/execution_service.py`.
