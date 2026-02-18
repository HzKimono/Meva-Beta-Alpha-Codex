# BtcTurk Adapter Audit

## Endpoint inventory

### Legacy synchronous adapter (`adapters/btcturk_http.py`)

#### Public endpoints

- `GET /api/v2/server/exchangeinfo`
  - Used by `get_exchange_info()` for pair metadata/rules and `health_check()`.
  - Downstream consumers: market-data rules (`MarketDataService`, Stage3/4 rules services), startup health command.
- `GET /api/v2/orderbook?pairSymbol=...` (optional `limit`)
  - Used by `get_orderbook()` for best bid/ask.
  - Downstream consumers: Stage3 market reads, universe filtering, account snapshot pricing.
- `GET /api/v2/ticker`
  - Used by `get_ticker_stats()` (mostly Stage7 analytics/universe/perf calculations).

#### Private endpoints

- `GET /api/v1/users/balances`
  - Used by `get_balances()`; feeds cash/exposure/account snapshots.
- `GET /api/v1/openOrders?pairSymbol=...`
  - Used by `get_open_orders()` and stage4 wrappers; core open-order state/reconcile input.
- `GET /api/v1/allOrders?pairSymbol=...&startDate=...&endDate=...`
  - Used by `get_all_orders()`; submit/cancel reconciliation fallback.
- `GET /api/v1/order/{order_id}`
  - Used by `get_order()`; point lookup in reconcile paths.
- `GET /api/v1/users/transactions/trade?pairSymbol=...&startDate=...`
  - Used by `get_recent_fills()` and stage4 fill adapters.
- `POST /api/v1/order`
  - Used for limit order submit (`_submit_limit_order_legacy`, `submit_limit_order`, Stage3 `place_limit_order`).
  - Payload includes `newOrderClientId` for idempotent identification.
- `DELETE /api/v1/order` with `{id: ...}`
  - Used by `cancel_order()` and `cancel_order_by_exchange_id()`.
- `DELETE /api/v1/order` with `{orderClientId: ...}`
  - Used by `cancel_order_by_client_order_id()`.

### New async reliability adapter (`adapters/btcturk/rest_client.py`)

- Generic request wrapper over private/public REST with reliability policy.
- Explicit operation helpers currently use:
  - `POST /api/v1/order` (`submit_order_safe`)
  - `DELETE /api/v1/order/{order_id}` (`cancel_order_safe`)
  - `GET /api/v1/openOrders` (`find_open_order_by_client_order_id`, `is_order_open`)

### WebSocket integration

- Config default URL: `wss://ws-feed-pro.btcturk.com`.
- `BtcturkWsClient` is generic transport/subscription handling; domain event channels are interpreted by reconciliation layer (`reconcile.py`) but URL/channel wiring is runtime-config driven.

## Auth/signing correctness checklist

### Implemented signing flow

- Header names: `X-PCK`, `X-Stamp`, `X-Signature`.
- Signature input message: `api_key + stamp_ms`.
- HMAC algorithm: HMAC-SHA256.
- Secret handling: API secret is base64-decoded before HMAC key usage.
- Signature output: base64-encoded digest.
- Stamp monotonicity:
  - sync path: `MonotonicNonceGenerator` ensures strictly increasing millisecond stamps.
  - async path: `ClockSyncService` applies server-time offset and periodically syncs.

### Checklist

- ✅ Required private auth headers are present and consistently built.
- ✅ HMAC-SHA256 + base64(secret) + base64(signature) matches BtcTurk scheme.
- ✅ Request IDs/correlation IDs are attached (`X-Request-ID` or `X-Correlation-ID`) for traceability.
- ✅ Private requests reject missing credentials early (`ConfigurationError`).
- ⚠️ Sync private mutation path (`_private_request`) is not using the generalized retry classifier; only private **GET** path has explicit retry loop.
- ⚠️ Clock skew handling is stronger in async stack than in legacy sync stack (monotonic != server-synced).

## Rate-limit strategy

### Current behavior

- **Legacy sync client (`BtcturkHttpClient`)**
  - Retries public requests via `retry_with_backoff` on timeout/transport/http status errors.
  - Private GET retries on `{429,500,502,503,504}` with capped total wait budget.
  - Honors `Retry-After` where available.
  - Emits retry/429 metrics.
  - No proactive token-bucket throttle before sending requests.

- **Async reliability client (`BtcturkRestClient`)**
  - Proactive throttling via `AsyncTokenBucket.acquire()` before each request.
  - Retry classification by error kind (`NETWORK`, `SERVER`, `RATE_LIMIT`) with jittered exponential backoff and `Retry-After` override.
  - Safe submit/cancel semantics reconcile before retrying effectful actions.

- **Stage7 OMS simulation path**
  - Separate `TokenBucketRateLimiter` for simulated OMS pacing and retry behavior.

### Assessment

- The strongest rate-limit strategy exists in the async adapter, but production Stage3/4 flow still primarily uses legacy sync adapter via `exchange_factory`.
- Recommendation: converge live write path onto one reliability/throttle policy (prefer async-style token bucket + classified retries).

## Precision/normalization notes

### Symbol/pair normalization

- Canonicalization uses `normalize_symbol`/`canonical_symbol` across adapters/services.
- Pair fields accepted from multiple exchange payload variants (`pairSymbol`, `pairSymbolNormalized`, `nameNormalized`) and normalized before domain usage.
- `_pair_symbol()` in adapter currently forwards normalized symbol directly.

### Precision and min-notional handling

- Price/qty are quantized with exchange rules (`quantize_price`, `quantize_quantity`) and validated (`validate_order`) before submit.
- `submit_limit_order` applies both rule-based validation and explicit min-notional check.
- If exchange info is unavailable, fallback pair rules are synthesized with default min-notional TRY 10.

### Risks / caveats

- Fallback rules (especially default min-notional/scale) may diverge from live exchange constraints for certain pairs.
- Dynamic universe service has a private `_get("/api/v2/orderbook")` optimization path; this bypasses a narrower typed port and couples service to adapter internals.

## Response parsing, error mapping, exception taxonomy

### Parsing

- Legacy adapter performs strict payload shape checks for list/object forms and raises `ValueError` for malformed payloads.
- Order/status parsing maps exchange raw status strings to `ExchangeOrderStatus` with tolerant aliases (`untouched/open/partial/filled/canceled/cancelled/rejected`).
- Numeric conversion uses decimal-safe parsing helpers to reduce float drift.

### Exception taxonomy in practice

- `ConfigurationError`: missing API credentials.
- `ValidationError`: order/rule violations before submit.
- `ExchangeError`: HTTP/non-success exchange responses with mapped fields (status/code/message/path/method/sanitized request snippets).
- `ValueError`: malformed response payload structures.
- Async stack adds `RestRequestError(kind=NETWORK|RATE_LIMIT|SERVER|CLIENT|EXCHANGE)` and maps final failure back to `ExchangeError`.

### Assessment

- Taxonomy is mostly sound but split between legacy and async stacks.
- Recommendation: unify around a shared adapter exception model (transport vs protocol/payload vs business validation) and map consistently at boundaries.

## Recommended adapter interface (ports/adapters split)

### Proposed ports (domain-facing)

1. `MarketDataPort`
   - `get_top_of_book(symbol) -> TopOfBook`
   - `get_ticker_stats() -> list[TickerStat]`
   - `get_exchange_rules() -> list[SymbolRules]`

2. `AccountPort`
   - `get_balances() -> list[Balance]`
   - `get_recent_fills(symbol, since_ms) -> list[TradeFill]`

3. `OrderExecutionPort`
   - `submit_limit(request: SubmitOrder) -> SubmitAck`
   - `cancel_by_order_id(order_id) -> CancelAck`
   - `cancel_by_client_id(client_id) -> CancelAck`
   - `get_open_orders(symbol) -> list[OpenOrder]`
   - `get_order_history(symbol, start_ms, end_ms) -> list[OrderSnapshot]`

4. `StreamingPort` (optional)
   - `subscribe_orderbook(...)`
   - `subscribe_trades(...)`
   - `subscribe_order_updates(...)`

### Domain vs infrastructure ownership

- **Domain layer should own**:
  - canonical symbols, order side/status enums, typed order intent/request/ack models,
  - deterministic idempotency keys/client-order-id generation policy,
  - rule validation invariants (min notional, step/tick constraints as generic contracts).

- **Infrastructure layer should own**:
  - endpoint URLs, auth headers/signature, nonce/time sync,
  - retry/backoff/rate-limit/throttle policies,
  - raw payload parsing and mapping from exchange-specific JSON into domain DTOs,
  - websocket connection/reconnect/backpressure mechanics.

### Refactor direction

- Keep a thin `BtcturkAdapter` implementation of the three ports above.
- Route Stage3/4 execution through the same reliability stack used by async rest client semantics (safe submit/cancel reconciliation).
- Disallow service-level calls to adapter internals (`_get`, raw payload probing); expose typed methods on ports instead.
