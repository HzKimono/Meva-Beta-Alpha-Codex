from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from random import Random
from threading import Event, Lock
from time import monotonic, sleep, time
from uuid import uuid4

import httpx

from btcbot.adapters.btcturk_auth import MonotonicNonceGenerator, build_auth_headers
from btcbot.adapters.exchange import ExchangeClient
from btcbot.adapters.exchange_stage4 import ExchangeClientStage4, OrderAck
from btcbot.domain.accounting import TradeFill
from btcbot.domain.models import (
    Balance,
    BtcturkBalanceItem,
    CancelOrderResult,
    ExchangeError,
    ExchangeOrderStatus,
    OpenOrderItem,
    OpenOrders,
    Order,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    PairInfo,
    SubmitOrderRequest,
    SubmitOrderResult,
    ValidationError,
    normalize_symbol,
    pair_info_to_symbol_rules,
    parse_decimal,
    quantize_price,
    quantize_quantity,
    validate_order,
)
from btcbot.domain.stage4 import Order as Stage4Order
from btcbot.observability import get_instrumentation
from btcbot.security.redaction import sanitize_mapping, sanitize_text
from btcbot.services.rate_limiter import EndpointBudget, TokenBucketRateLimiter, map_endpoint_group
from btcbot.services.retry import parse_retry_after_seconds, retry_with_backoff

logger = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


_PRIVATE_ERROR_SNIPPET_LIMIT = 240
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY_SECONDS = 0.4
_RETRY_MAX_DELAY_SECONDS = 4.0
_RETRY_TOTAL_WAIT_CAP_SECONDS = 8.0
_DEFAULT_MIN_NOTIONAL_TRY = Decimal("10")
_BREAKER_CONSECUTIVE_429_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 3.0


@dataclass
class _BreakerState:
    consecutive_429: int = 0
    open_until: float = 0.0


class _RetryableRequestError(Exception):
    def __init__(
        self,
        exchange_error: ExchangeError,
        *,
        retry_after_header: str | None = None,
    ) -> None:
        super().__init__(str(exchange_error))
        self.exchange_error = exchange_error
        self.retry_after_header = retry_after_header


def _is_permanent_transport_error(exc: httpx.TransportError) -> bool:
    permanent_errors = (
        httpx.UnsupportedProtocol,
        httpx.ProtocolError,
        httpx.LocalProtocolError,
    )
    if isinstance(exc, permanent_errors):
        return True

    cause = getattr(exc, "__cause__", None)
    return isinstance(cause, ssl.SSLCertVerificationError)


def _retry_delay_seconds(attempt: int, *, response: httpx.Response | None = None) -> float:
    if response is not None and response.status_code == 429:
        retry_after = parse_retry_after_seconds(response.headers.get("Retry-After"))
        if retry_after is not None:
            return min(retry_after, _RETRY_MAX_DELAY_SECONDS)

    backoff = _RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempt - 1))
    return min(backoff, _RETRY_MAX_DELAY_SECONDS)


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return not _is_permanent_transport_error(exc)
    return False


def _parse_best_price(levels: object, side: str, symbol: str) -> Decimal:
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"No orderbook {side} depth for {symbol}")

    top_level = levels[0]
    if not isinstance(top_level, list) or not top_level:
        raise ValueError(f"Malformed orderbook {side} level for {symbol}")

    try:
        value = parse_decimal(top_level[0])
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"Invalid orderbook {side} price for {symbol}") from exc

    if value <= Decimal("0") or not isfinite(float(value)):
        raise ValueError(f"Non-positive orderbook {side} price for {symbol}")
    return value


def _response_snippet(response: httpx.Response) -> str:
    text = response.text.strip().replace("\n", " ")
    return sanitize_text(text[:_PRIVATE_ERROR_SNIPPET_LIMIT])


def _sanitize_request_params(params: dict[str, str | int] | None) -> dict[str, object] | None:
    if params is None:
        return None
    return sanitize_mapping(params)


def _sanitize_request_json(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    return sanitize_mapping(payload)



def _fmt_decimal(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _btcturk_pair_symbol(symbol: str) -> str:
    return normalize_symbol(symbol).replace("_", "")


class BtcturkHttpClient(ExchangeClient):
    BASE_URL = "https://api.btcturk.com"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        breaker_429_consecutive_threshold: int = _BREAKER_CONSECUTIVE_429_THRESHOLD,
        breaker_cooldown_seconds: float = _BREAKER_COOLDOWN_SECONDS,
        orderbook_cache_ttl_s: float = 0.2,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        resolved_timeout = (
            timeout
            if isinstance(timeout, httpx.Timeout)
            else httpx.Timeout(timeout=timeout, connect=5.0, read=10.0, write=10.0, pool=5.0)
        )
        self.client = httpx.Client(
            base_url=base_url or self.BASE_URL,
            timeout=resolved_timeout,
            transport=transport,
        )
        self._nonce = MonotonicNonceGenerator()
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            {
                "default": EndpointBudget(name="default", rps=8.0, burst=8),
                "market_data": EndpointBudget(name="market_data", rps=8.0, burst=8),
                "account": EndpointBudget(name="account", rps=4.0, burst=4),
                "orders": EndpointBudget(name="orders", rps=2.0, burst=2),
            }
        )
        self._breaker_429_consecutive_threshold = max(1, breaker_429_consecutive_threshold)
        self._breaker_cooldown_seconds = max(0.0, breaker_cooldown_seconds)
        self._breaker_state: dict[str, _BreakerState] = {}
        self._orderbook_cache_ttl_s = max(0.0, orderbook_cache_ttl_s)
        self._orderbook_cache: dict[tuple[str, int | None], tuple[float, tuple[Decimal, Decimal]]] = {}
        self._orderbook_inflight: dict[tuple[str, int | None], Event] = {}
        self._orderbook_lock = Lock()

    def __enter__(self) -> BtcturkHttpClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.close()

    def _request_group(self, path: str) -> str:
        return map_endpoint_group(path)

    def _breaker_for(self, group: str) -> _BreakerState:
        state = self._breaker_state.get(group)
        if state is None:
            state = _BreakerState()
            self._breaker_state[group] = state
        return state

    def _check_breaker(self, group: str) -> None:
        state = self._breaker_for(group)
        now = monotonic()
        if state.open_until > now:
            get_instrumentation().counter("breaker_open_total", 1, attrs={"group": group})
            raise ExchangeError(
                f"Circuit breaker open for group={group}",
                status_code=429,
                error_code="breaker_open",
            )

    def _record_success(self, group: str) -> None:
        self._breaker_for(group).consecutive_429 = 0

    def _record_429(self, group: str, retry_after_s: float | None) -> None:
        state = self._breaker_for(group)
        state.consecutive_429 += 1
        self._rate_limiter.penalize_on_429(group, retry_after_s)
        if state.consecutive_429 >= self._breaker_429_consecutive_threshold:
            cooldown = retry_after_s if retry_after_s is not None else self._breaker_cooldown_seconds
            state.open_until = max(state.open_until, monotonic() + cooldown)
            get_instrumentation().counter("breaker_open_total", 1, attrs={"group": group})

    def _get(self, path: str, params: dict[str, str | int] | None = None) -> dict:
        group = self._request_group(path)
        request_id = uuid4().hex

        def _call() -> dict:
            self._check_breaker(group)
            waited = self._rate_limiter.acquire(group)
            get_instrumentation().histogram("rate_limiter_wait_seconds", waited, attrs={"group": group})
            with get_instrumentation().trace("rest_call", attrs={"method": "GET", "path": path, "group": group}):
                response = self.client.get(
                    path,
                    params=params,
                    headers={"X-Request-ID": request_id},
                )
            get_instrumentation().counter(
                "rest_requests_total",
                1,
                attrs={"group": group, "path": path, "status": str(response.status_code)},
            )
            if response.status_code >= 500 or response.status_code == 429:
                response.raise_for_status()
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise ExchangeError(
                    f"BTCTurk public endpoint error status={response.status_code} path={path}",
                    status_code=response.status_code,
                    request_path=path,
                    request_method="GET",
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("BTCTurk response payload must be a JSON object")
            if payload.get("success") is False:
                message = payload.get("message") or payload.get("code") or "unknown BTCTurk error"
                raise ValueError(
                    f"BTCTurk API returned unsuccessful payload: {message}; request_id={request_id}"
                )
            self._record_success(group)
            return payload

        def _retry_after(exc: Exception) -> str | None:
            response = getattr(exc, "response", None)
            if response is None:
                return None
            return response.headers.get("Retry-After")

        def _on_retry(attempt: object) -> None:
            get_instrumentation().counter("rest_retry_total", 1, attrs={"group": group, "path": path})

        try:
            return retry_with_backoff(
                _call,
                max_attempts=_RETRY_ATTEMPTS,
                base_delay_ms=int(_RETRY_BASE_DELAY_SECONDS * 1000),
                max_delay_ms=int(_RETRY_MAX_DELAY_SECONDS * 1000),
                max_total_sleep_seconds=_RETRY_TOTAL_WAIT_CAP_SECONDS,
                jitter_seed=17,
                retry_on_exceptions=(
                    httpx.TimeoutException,
                    httpx.TransportError,
                    httpx.HTTPStatusError,
                ),
                retry_after_getter=_retry_after,
                on_retry=_on_retry,
                sleep_fn=self._safe_sleep,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                retry_after_s = parse_retry_after_seconds(exc.response.headers.get("Retry-After"))
                self._record_429(group, retry_after_s)
                get_instrumentation().counter("rest_429_total", 1, attrs={"group": group, "path": path})
            raise

    def _safe_sleep(self, seconds: float) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            sleep(seconds)
            return
        raise RuntimeError("Blocking retry sleep called from an active event loop")

    def _next_stamp_ms(self) -> str:
        return str(self._nonce.next_stamp_ms())

    def _private_request(
        self,
        method: str,
        path: str,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict:
        if not self.api_key or not self.api_secret:
            raise ConfigurationError(
                "Missing BTCTURK API credentials: "
                "BTCTURK_API_KEY and BTCTURK_API_SECRET are required for private endpoints"
            )

        group = self._request_group(path)
        request_id = uuid4().hex
        normalized_method = method.upper()
        is_private_write = normalized_method in {"POST", "PUT", "PATCH", "DELETE"}

        def _call() -> dict:
            self._check_breaker(group)
            waited = self._rate_limiter.acquire(group)
            get_instrumentation().histogram("rate_limiter_wait_seconds", waited, attrs={"group": group})
            headers = build_auth_headers(
                api_key=self.api_key or "",
                api_secret=self.api_secret or "",
                stamp_ms=self._next_stamp_ms(),
            )
            headers["X-Request-ID"] = request_id
            with get_instrumentation().trace(
                "rest_call", attrs={"method": normalized_method, "path": path, "group": group}
            ):
                response = self.client.request(
                    method=normalized_method,
                    url=path,
                    params=params,
                    json=json,
                    headers=headers,
                )
            get_instrumentation().counter(
                "rest_requests_total",
                1,
                attrs={"group": group, "path": path, "status": str(response.status_code)},
            )
            if response.status_code != 200:
                snippet = _response_snippet(response)
                payload_code = None
                payload_message = None
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        payload_code = payload.get("code")
                        payload_message = payload.get("message")
                except Exception:  # noqa: BLE001
                    payload = None

                safe_payload_message = (
                    sanitize_text(str(payload_message)) if payload_message is not None else None
                )
                err = ExchangeError(
                    "BTCTurk private endpoint error "
                    f"status={response.status_code} method={normalized_method} path={path} "
                    f"code={payload_code} message={safe_payload_message} response={snippet} "
                    f"request_has_params={params is not None} request_has_json={json is not None} "
                    f"request_id={request_id}",
                    status_code=response.status_code,
                    error_code=payload_code,
                    error_message=safe_payload_message,
                    request_path=path,
                    request_method=normalized_method,
                    request_params=_sanitize_request_params(params),
                    request_json=_sanitize_request_json(json),
                    response_body=snippet,
                )
                should_retry_status = response.status_code == 429 or (
                    response.status_code >= 500 and not is_private_write
                )
                if should_retry_status:
                    retry_after_header = response.headers.get("Retry-After")
                    if response.status_code == 429:
                        self._record_429(group, parse_retry_after_seconds(retry_after_header))
                        get_instrumentation().counter(
                            "rest_429_total",
                            1,
                            attrs={"group": group, "path": path},
                        )
                    raise _RetryableRequestError(
                        err,
                        retry_after_header=retry_after_header,
                    ) from err
                raise err

            payload = response.json()
            if not isinstance(payload, dict):
                raise ExchangeError("BTCTurk response payload must be a JSON object")
            if payload.get("success") is False:
                message = payload.get("message") or payload.get("code") or "unknown BTCTurk error"
                safe_message = sanitize_text(str(message))
                raise ExchangeError(
                    f"BTCTurk API returned unsuccessful payload: {safe_message}; request_id={request_id}",
                    status_code=200,
                    error_code=payload.get("code"),
                    error_message=safe_message,
                    request_path=path,
                    request_method=normalized_method,
                    request_params=_sanitize_request_params(params),
                    request_json=_sanitize_request_json(json),
                )
            self._record_success(group)
            return payload

        def _retry_after(exc: Exception) -> str | None:
            if isinstance(exc, _RetryableRequestError):
                return exc.retry_after_header
            if isinstance(exc, ExchangeError) and exc.response_body is not None:
                return None
            response = getattr(exc, "response", None)
            if response is None:
                return None
            return response.headers.get("Retry-After")

        def _on_retry(_attempt: object) -> None:
            get_instrumentation().counter("rest_retry_total", 1, attrs={"group": group, "path": path})

        try:
            return retry_with_backoff(
                _call,
                max_attempts=_RETRY_ATTEMPTS,
                base_delay_ms=int(_RETRY_BASE_DELAY_SECONDS * 1000),
                max_delay_ms=int(_RETRY_MAX_DELAY_SECONDS * 1000),
                max_total_sleep_seconds=_RETRY_TOTAL_WAIT_CAP_SECONDS,
                jitter_seed=23,
                retry_on_exceptions=(_RetryableRequestError, httpx.TimeoutException, httpx.TransportError),
                retry_after_getter=_retry_after,
                on_retry=_on_retry,
                sleep_fn=self._safe_sleep,
            )
        except _RetryableRequestError as exc:
            raise exc.exchange_error from exc

    def _private_get(self, path: str, params: dict[str, str | int] | None = None) -> dict:
        return self._private_request("GET", path, params=params)

    def get_recent_fills(self, pair_symbol: str, since_ms: int | None = None) -> list[TradeFill]:
        params: dict[str, str | int] = {"pairSymbol": self._pair_symbol(pair_symbol)}
        if since_ms is not None:
            params["startDate"] = since_ms
        payload = self._private_get("/api/v1/users/transactions/trade", params=params)
        rows = self._extract_fill_rows(payload, path="/api/v1/users/transactions/trade")
        fills: list[TradeFill] = []
        for row in rows:
            fill_id = str(row.get("id") or row.get("orderClientId") or row.get("orderId"))
            order_id = str(row.get("orderId") or row.get("id"))
            side = self._parse_side({"type": row.get("orderType") or row.get("type")})
            if side is None:
                continue
            ts_ms = int(row.get("timestamp") or row.get("date") or int(time() * 1000))
            fills.append(
                TradeFill(
                    fill_id=fill_id,
                    order_id=order_id,
                    symbol=pair_symbol,
                    side=side,
                    price=parse_decimal(row.get("price")),
                    qty=parse_decimal(row.get("amount") or row.get("quantity")),
                    fee=parse_decimal(row.get("fee") or 0),
                    fee_currency=str(row.get("feeCurrency") or "TRY"),
                    ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                )
            )
        return fills

    def _extract_fill_rows(self, payload: dict, *, path: str) -> list[dict[str, object]]:
        data = payload.get("data")
        if data is None:
            return []

        rows: list[object] | None = None
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            data_items = data.get("items")
            if isinstance(data_items, list):
                rows = data_items
            else:
                symbols = data.get("symbols")
                if isinstance(symbols, list):
                    rows = symbols
                elif not data:
                    rows = []
                else:
                    return []
        elif not data:
            rows = []
        else:
            raise ValueError(f"Malformed list payload for {path}: {type(data).__name__}")

        normalized: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Malformed item in {path} payload: {row!r}")
            normalized.append(row)
        return normalized

    def _pair_symbol(self, symbol: str) -> str:
        return _btcturk_pair_symbol(symbol)

    def _extract_list_data(self, payload: dict, *, path: str) -> list[dict[str, object]]:
        data = payload.get("data")
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            data_items = data.get("items")
            if isinstance(data_items, list):
                rows = data_items
            else:
                symbols = data.get("symbols")
                rows = symbols if isinstance(symbols, list) else []
        else:
            rows = []

        if not rows:
            raise ValueError(f"BTCTurk payload did not include list data for {path}")

        normalized: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Malformed item in {path} payload: {row!r}")
            normalized.append(row)
        return normalized

    def _extract_order_rows(self, payload: dict, *, path: str) -> list[dict[str, object]]:
        data = payload.get("data")
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("items") if isinstance(data.get("items"), list) else [data]
        else:
            raise ValueError(f"Malformed order payload for {path}")

        normalized: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Malformed item in {path} payload: {row!r}")
            normalized.append(row)
        return normalized

    def _parse_side(self, item: dict[str, object]) -> OrderSide | None:
        for key in ("orderMethod", "method", "orderType", "type"):
            value = item.get(key)
            if value is None:
                continue
            normalized = str(value).strip().lower()
            if normalized == "buy":
                return OrderSide.BUY
            if normalized == "sell":
                return OrderSide.SELL
        return None

    def _parse_exchange_status(
        self, item: dict[str, object]
    ) -> tuple[ExchangeOrderStatus, str | None]:
        raw = None
        for key in ("status", "orderStatus", "state"):
            if item.get(key) is not None:
                raw = str(item.get(key))
                break
        if raw is None:
            return (ExchangeOrderStatus.UNKNOWN, None)

        normalized = raw.strip().lower()
        mapping = {
            "untouched": ExchangeOrderStatus.OPEN,
            "open": ExchangeOrderStatus.OPEN,
            "partial": ExchangeOrderStatus.PARTIAL,
            "partiallyfilled": ExchangeOrderStatus.PARTIAL,
            "filled": ExchangeOrderStatus.FILLED,
            "completed": ExchangeOrderStatus.FILLED,
            "canceled": ExchangeOrderStatus.CANCELED,
            "cancelled": ExchangeOrderStatus.CANCELED,
            "rejected": ExchangeOrderStatus.REJECTED,
        }
        return (mapping.get(normalized, ExchangeOrderStatus.UNKNOWN), raw)

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[Decimal, Decimal]:
        pair_symbol = self._pair_symbol(symbol)
        key = (pair_symbol, limit)

        with self._orderbook_lock:
            cached = self._orderbook_cache.get(key)
            now = monotonic()
            if cached is not None and cached[0] > now:
                get_instrumentation().counter("orderbook_cache_hits_total", 1, attrs={"pair_symbol": pair_symbol})
                return cached[1]
            get_instrumentation().counter("orderbook_cache_misses_total", 1, attrs={"pair_symbol": pair_symbol})
            inflight = self._orderbook_inflight.get(key)
            if inflight is None:
                inflight = Event()
                self._orderbook_inflight[key] = inflight
                leader = True
            else:
                leader = False
                get_instrumentation().counter("orderbook_inflight_joins_total", 1, attrs={"pair_symbol": pair_symbol})

        if not leader:
            inflight.wait()
            with self._orderbook_lock:
                cached = self._orderbook_cache.get(key)
                if cached is not None and cached[0] > monotonic():
                    return cached[1]
            raise ExchangeError(f"Orderbook inflight request failed for {pair_symbol}")

        try:
            params: dict[str, str | int] = {"pairSymbol": pair_symbol}
            if limit is not None:
                params["limit"] = limit

            path = "/api/v2/orderbook"
            payload = self._get(path, params=params)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError(f"Malformed orderbook payload for {symbol}: data must be an object")

            best_bid = _parse_best_price(data.get("bids"), side="bid", symbol=symbol)
            best_ask = _parse_best_price(data.get("asks"), side="ask", symbol=symbol)
            result = (best_bid, best_ask)
            with self._orderbook_lock:
                self._orderbook_cache[key] = (monotonic() + self._orderbook_cache_ttl_s, result)
            return result
        finally:
            with self._orderbook_lock:
                event = self._orderbook_inflight.pop(key, None)
            if event is not None:
                event.set()

    def get_ticker_stats(self) -> list[dict[str, object]]:
        payload = self._get("/api/v2/ticker")
        return self._extract_list_data(payload, path="/api/v2/ticker")

    def get_candles(self, symbol: str, limit: int) -> list[dict[str, object]]:
        del symbol, limit
        return []

    def _to_pair_info(self, item: dict[str, object]) -> PairInfo:
        try:
            min_total = item.get("minTotalAmount")
            min_total = item.get("minExchangeValue", min_total)
            min_price = item.get("minPrice")
            max_price = item.get("maxPrice")
            min_price = item.get("minimumLimitOrderPrice", min_price)
            max_price = item.get("maximumLimitOrderPrice", max_price)
            min_qty = item.get("minQuantity")
            max_qty = item.get("maxQuantity")
            tick_size = item.get("tickSize")
            step_size = item.get("stepSize")

            filters = item.get("filters")
            if isinstance(filters, list):
                for flt in filters:
                    if not isinstance(flt, dict):
                        continue
                    filter_type = str(flt.get("filterType", "")).upper()
                    if filter_type == "PRICE_FILTER":
                        min_price = flt.get("minPrice", min_price)
                        max_price = flt.get("maxPrice", max_price)
                        tick_size = flt.get("tickSize", tick_size)
                        min_total = flt.get("minExchangeValue", min_total)
                        min_total = flt.get("minAmount", min_total)
                    elif filter_type in {"QUANTITY_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE"}:
                        min_qty = flt.get("minQuantity", min_qty)
                        min_qty = flt.get("minQty", min_qty)
                        max_qty = flt.get("maxQuantity", max_qty)
                        max_qty = flt.get("maxQty", max_qty)
                        step_size = flt.get("stepSize", step_size)
                        step_size = flt.get("lotSize", step_size)
                    elif filter_type in {"MIN_TOTAL", "MIN_NOTIONAL", "NOTIONAL"}:
                        min_total = flt.get("minTotalAmount", min_total)
                        min_total = flt.get("minExchangeValue", min_total)
                        min_total = flt.get("minAmount", min_total)
                        min_total = flt.get("minNotional", min_total)

            pair_symbol = self._resolve_pair_symbol(item)

            min_price_dec = parse_decimal(min_price) if min_price is not None else None
            max_price_dec = parse_decimal(max_price) if max_price is not None else None
            min_qty_dec = parse_decimal(min_qty) if min_qty is not None else None
            max_qty_dec = parse_decimal(max_qty) if max_qty is not None else None
            min_total_dec = parse_decimal(min_total) if min_total is not None else None
            tick_size_dec = parse_decimal(tick_size) if tick_size is not None else None
            step_size_dec = parse_decimal(step_size) if step_size is not None else None

            denominator_scale = self._resolve_scale(
                item.get("denominatorScale"),
                explicit_decimals=[tick_size_dec, min_price_dec, max_price_dec],
                default=8,
            )
            numerator_scale = self._resolve_scale(
                item.get("numeratorScale"),
                explicit_decimals=[min_qty_dec, max_qty_dec],
                default=8,
            )

            if not isinstance(filters, list):
                logger.warning(
                    "Exchange info item missing filters; using conservative defaults",
                    extra={"extra": {"pair_symbol": pair_symbol}},
                )

            return PairInfo(
                pairSymbol=str(pair_symbol),
                name=(str(item["name"]) if item.get("name") is not None else None),
                nameNormalized=(
                    str(item["nameNormalized"])
                    if item.get("nameNormalized") is not None
                    else (
                        normalize_symbol(str(item["pairSymbolNormalized"]))
                        if item.get("pairSymbolNormalized") is not None
                        else None
                    )
                ),
                status=(str(item["status"]) if item.get("status") is not None else None),
                numerator=(str(item["numerator"]) if item.get("numerator") is not None else None),
                denominator=(
                    str(item["denominator"]) if item.get("denominator") is not None else None
                ),
                numeratorScale=numerator_scale,
                denominatorScale=denominator_scale,
                minTotalAmount=min_total_dec,
                min_price=min_price_dec,
                max_price=max_price_dec,
                minQuantity=min_qty_dec,
                maxQuantity=max_qty_dec,
                tickSize=tick_size_dec,
                stepSize=step_size_dec,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"Malformed exchange info pair item: {item}") from exc

    def _resolve_pair_symbol(self, item: dict[str, object]) -> str:
        raw_symbol = item.get("pairSymbol")
        if raw_symbol is None:
            raw_symbol = item.get("pairSymbolNormalized") or item.get("nameNormalized") or item.get("name")
        if raw_symbol is None:
            keys = sorted(item.keys())
            raise ValueError(
                "exchangeinfo pair is missing symbol fields; expected one of "
                "'pairSymbol', 'pairSymbolNormalized', 'nameNormalized', 'name'; "
                f"received keys={keys}"
            )

        return _btcturk_pair_symbol(str(raw_symbol))

    def _resolve_scale(
        self,
        raw_scale: object,
        *,
        explicit_decimals: list[Decimal | None],
        default: int,
    ) -> int:
        if raw_scale is not None:
            return int(raw_scale)

        decimal_values = [value for value in explicit_decimals if value is not None]
        if not decimal_values:
            return default

        scales: list[int] = []
        for value in decimal_values:
            exponent = value.normalize().as_tuple().exponent
            scales.append(max(0, -exponent))
        return max(scales) if scales else default

    def get_exchange_info(self) -> list[PairInfo]:
        path = "/api/v2/server/exchangeinfo"
        try:
            payload = self._get(path)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            snippet = _response_snippet(response) if response is not None else ""
            status = response.status_code if response is not None else "unknown"
            raise ValueError(
                f"BTCTurk public endpoint error status={status} path={path} response={snippet}"
            ) from exc

        rows = self._extract_list_data(payload, path=path)
        pairs: list[PairInfo] = []
        malformed = 0
        for index, item in enumerate(rows):
            try:
                pairs.append(self._to_pair_info(item))
            except ValueError as exc:
                malformed += 1
                logger.warning(
                    "Skipping malformed exchange info row",
                    extra={
                        "extra": {
                            "row_index": index,
                            "error_type": type(exc).__name__,
                            "safe_message": "exchange info pair parse failed",
                        }
                    },
                )
        if not pairs:
            raise ValueError(
                "Malformed exchange info: all rows invalid "
                f"(rows={len(rows)}, malformed={malformed})"
            )
        return pairs

    def health_check(self) -> bool:
        data = self._get("/api/v2/server/exchangeinfo")
        return data.get("success") is True

    def _to_balance_item(self, item: dict[str, object]) -> BtcturkBalanceItem:
        try:
            return BtcturkBalanceItem(
                asset=str(item["asset"]),
                balance=parse_decimal(item["balance"]),
                locked=parse_decimal(item["locked"]),
                free=parse_decimal(item["free"]),
                orderFund=(
                    parse_decimal(item["orderFund"]) if item.get("orderFund") is not None else None
                ),
                requestFund=(
                    parse_decimal(item["requestFund"])
                    if item.get("requestFund") is not None
                    else None
                ),
                precision=(int(item["precision"]) if item.get("precision") is not None else None),
                timestamp=(int(item["timestamp"]) if item.get("timestamp") is not None else None),
                assetname=(str(item["assetname"]) if item.get("assetname") is not None else None),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"Malformed balance item: {item}") from exc

    def _to_open_order_item(self, item: dict[str, object]) -> OpenOrderItem:
        try:
            return OpenOrderItem(
                id=int(item["id"]),
                price=parse_decimal(item["price"]),
                amount=parse_decimal(item.get("amount", item.get("quantity", "0"))),
                quantity=parse_decimal(item["quantity"]),
                stopPrice=(
                    parse_decimal(item["stopPrice"]) if item.get("stopPrice") is not None else None
                ),
                pairSymbol=str(item["pairSymbol"]),
                pairSymbolNormalized=str(item.get("pairSymbolNormalized", item["pairSymbol"])),
                type=str(item.get("type", "limit")),
                method=str(item.get("method", item.get("orderMethod", ""))),
                orderClientId=(
                    str(item["orderClientId"]) if item.get("orderClientId") is not None else None
                ),
                time=int(item["time"]),
                updateTime=(
                    int(item["updateTime"]) if item.get("updateTime") is not None else None
                ),
                status=str(item.get("status", "unknown")),
                leftAmount=(
                    parse_decimal(item["leftAmount"])
                    if item.get("leftAmount") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"Malformed open order item: {item}") from exc

    def _to_order_snapshot(self, item: dict[str, object]) -> OrderSnapshot:
        try:
            order_id_raw = item.get("id", item.get("orderId"))
            if order_id_raw is None:
                raise KeyError("id")

            status, status_raw = self._parse_exchange_status(item)
            side = self._parse_side(item)
            ts_raw = item.get("time", item.get("timestamp", item.get("createdAt", 0)))
            up_raw = item.get("updateTime", item.get("updatedAt"))
            return OrderSnapshot(
                order_id=str(order_id_raw),
                client_order_id=(
                    str(item["orderClientId"]) if item.get("orderClientId") is not None else None
                ),
                pair_symbol=str(item.get("pairSymbol", "")),
                side=side,
                price=parse_decimal(item.get("price", 0)),
                quantity=parse_decimal(item.get("quantity", item.get("amount", 0))),
                status=status,
                timestamp=int(ts_raw),
                update_time=int(up_raw) if up_raw is not None else None,
                status_raw=status_raw,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"Malformed order snapshot item: {item}") from exc

    def get_balances(self) -> list[Balance]:
        data = self._private_get("/api/v1/users/balances")
        balances_raw = data.get("data")
        if not isinstance(balances_raw, list):
            raise ValueError("Malformed balances payload")

        parsed: list[Balance] = []
        for raw in balances_raw:
            if not isinstance(raw, dict):
                raise ValueError("Malformed balances payload item")
            item = self._to_balance_item(raw)
            parsed.append(
                Balance(asset=item.asset, free=item.free, locked=item.locked)
            )
        return parsed

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        data = self._private_get(
            "/api/v1/openOrders", params={"pairSymbol": _btcturk_pair_symbol(pair_symbol)}
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise ValueError("Malformed open orders payload")

        bids_raw = payload.get("bids")
        asks_raw = payload.get("asks")
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            raise ValueError("Malformed open orders bids/asks payload")

        bids = [self._to_open_order_item(item) for item in bids_raw if isinstance(item, dict)]
        asks = [self._to_open_order_item(item) for item in asks_raw if isinstance(item, dict)]
        if len(bids) != len(bids_raw) or len(asks) != len(asks_raw):
            raise ValueError("Malformed open order item in payload")

        return OpenOrders(bids=bids, asks=asks)

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int) -> list[OrderSnapshot]:
        data = self._private_get(
            "/api/v1/allOrders",
            params={
                "pairSymbol": _btcturk_pair_symbol(pair_symbol),
                "startDate": start_ms,
                "endDate": end_ms,
            },
        )
        rows = self._extract_order_rows(data, path="/api/v1/allOrders")
        return [self._to_order_snapshot(item) for item in rows]

    def get_order(self, order_id: str) -> OrderSnapshot:
        data = self._private_get(f"/api/v1/order/{order_id}")
        rows = self._extract_order_rows(data, path=f"/api/v1/order/{order_id}")
        return self._to_order_snapshot(rows[0])

    def _submit_limit_order_legacy(self, request: SubmitOrderRequest) -> SubmitOrderResult:
        payload = self._build_submit_order_payload(request)
        response = self._private_request("POST", "/api/v1/order", json=payload)
        data = response.get("data")
        if not isinstance(data, dict) or data.get("id") is None:
            raise ExchangeError("Submit order response missing order id")
        return SubmitOrderResult(order_id=str(data["id"]))

    def _build_submit_order_payload(self, request: SubmitOrderRequest) -> dict[str, object]:
        return {
            "pairSymbol": _btcturk_pair_symbol(request.pair_symbol),
            "price": _fmt_decimal(request.price),
            "quantity": _fmt_decimal(request.quantity),
            "orderMethod": "limit",
            "orderType": request.side.value,
            "newOrderClientId": request.client_order_id,
        }

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_order_id: str,
    ) -> OrderAck:
        symbol_normalized = normalize_symbol(symbol)
        rules = self._resolve_symbol_rules(symbol_normalized)
        if price <= 0:
            raise ValidationError(f"price must be positive; observed={price}")
        if qty <= 0:
            raise ValidationError(f"quantity must be positive; observed={qty}")
        validate_order(price=price, qty=qty, rules=rules)
        quantized_price = quantize_price(price, rules)
        quantized_qty = quantize_quantity(qty, rules)
        if quantized_price <= 0:
            raise ValidationError(f"price non-positive after quantize; observed={quantized_price}")
        if quantized_qty <= 0:
            raise ValidationError(f"quantity non-positive after quantize; observed={quantized_qty}")
        computed_notional = quantized_price * quantized_qty
        min_notional = rules.min_total if rules.min_total is not None else _DEFAULT_MIN_NOTIONAL_TRY
        if computed_notional < min_notional:
            raise ValidationError(
                f"total below min_total for {rules.pair_symbol}; "
                f"required={min_notional} observed={computed_notional}"
            )

        request = SubmitOrderRequest(
            pair_symbol=_btcturk_pair_symbol(symbol_normalized),
            side=OrderSide(side.lower()),
            price=quantized_price,
            quantity=quantized_qty,
            client_order_id=client_order_id,
        )
        payload = self._build_submit_order_payload(request)
        try:
            response = self._private_request("POST", "/api/v1/order", json=payload)
        except ExchangeError as exc:
            logger.error(
                "BTCTurk submit_limit_order failed",
                extra={
                    "extra": {
                        "status_code": exc.status_code,
                        "error_code": exc.error_code,
                        "error_message": exc.error_message,
                        "request_method": exc.request_method,
                        "request_path": exc.request_path,
                        "request_json": exc.request_json,
                        "response_body": exc.response_body,
                        "pairSymbol": payload.get("pairSymbol"),
                        "orderType": payload.get("orderType"),
                        "orderMethod": payload.get("orderMethod"),
                        "price": payload.get("price"),
                        "quantity": payload.get("quantity"),
                        "clientOrderId": payload.get("newOrderClientId"),
                        "stopPrice": payload.get("stopPrice"),
                        "computed_notional_try": str(computed_notional),
                        "quantized_price": _fmt_decimal(quantized_price),
                        "quantized_qty": _fmt_decimal(quantized_qty),
                    }
                },
            )
            raise
        data = response.get("data")
        if not isinstance(data, dict) or data.get("id") is None:
            raise ExchangeError("Submit order response missing order id")
        return OrderAck(exchange_order_id=str(data["id"]), status="submitted", raw=data)

    def _resolve_symbol_rules(self, symbol: str):
        try:
            exchange_info = self.get_exchange_info()
        except Exception:  # noqa: BLE001
            exchange_info = []
        for pair in exchange_info:
            if normalize_symbol(pair.pair_symbol) == symbol:
                return pair_info_to_symbol_rules(pair)
        return pair_info_to_symbol_rules(
            PairInfo(
                pairSymbol=symbol,
                numeratorScale=8,
                denominatorScale=8,
                minTotalAmount=_DEFAULT_MIN_NOTIONAL_TRY,
            )
        )

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool:
        try:
            int(exchange_order_id)
        except ValueError as exc:
            raise ExchangeError(
                f"Invalid exchange_order_id for cancel: {exchange_order_id}"
            ) from exc
        return self.cancel_order(exchange_order_id)

    def cancel_order_by_client_order_id(self, client_order_id: str) -> bool:
        payload = {"orderClientId": client_order_id}
        response = self._private_request("DELETE", "/api/v1/order", json=payload)
        return bool(response.get("success") is True)

    def list_open_orders_stage4(self, symbol: str | None = None) -> list[Stage4Order]:
        stage3_orders = self.list_open_orders(symbol)
        return [
            Stage4Order(
                symbol=order.symbol,
                side=order.side.value,
                type="limit",
                price=order.price,
                qty=order.quantity,
                status=order.status.value,
                created_at=order.created_at,
                updated_at=order.updated_at,
                exchange_order_id=order.order_id,
                client_order_id=order.client_order_id,
                mode="live",
            )
            for order in stage3_orders
        ]

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> Order:
        if client_order_id is None:
            raise ValueError("client_order_id is required for BTCTurk place_limit_order")

        request = SubmitOrderRequest(
            pair_symbol=_btcturk_pair_symbol(symbol),
            side=side,
            price=price,
            quantity=quantity,
            client_order_id=client_order_id,
        )
        result = self._submit_limit_order_legacy(request)
        return Order(
            order_id=result.order_id,
            client_order_id=client_order_id,
            symbol=normalize_symbol(symbol),
            side=side,
            price=price,
            quantity=quantity,
            status=OrderStatus.OPEN,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def cancel_order(self, order_id: str) -> bool:
        response = self._private_request("DELETE", "/api/v1/order", json={"id": int(order_id)})
        result = CancelOrderResult(success=bool(response.get("success") is True))
        return result.success

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            return []

        response = self.get_open_orders(self._pair_symbol(symbol))
        all_items = [*response.bids, *response.asks]
        orders: list[Order] = []
        for item in all_items:
            raw = {
                "method": item.method,
                "type": item.type,
                "status": item.status,
            }
            side = self._parse_side(raw)
            if side is None:
                continue
            created = datetime.fromtimestamp(item.time / 1000, tz=UTC)
            updated_ms = item.update_time if item.update_time is not None else item.time
            updated = datetime.fromtimestamp(updated_ms / 1000, tz=UTC)
            status, _ = self._parse_exchange_status(raw)
            local_status = {
                ExchangeOrderStatus.OPEN: OrderStatus.OPEN,
                ExchangeOrderStatus.PARTIAL: OrderStatus.PARTIAL,
                ExchangeOrderStatus.FILLED: OrderStatus.FILLED,
                ExchangeOrderStatus.CANCELED: OrderStatus.CANCELED,
                ExchangeOrderStatus.REJECTED: OrderStatus.REJECTED,
            }.get(status, OrderStatus.UNKNOWN)
            orders.append(
                Order(
                    order_id=str(item.id),
                    client_order_id=item.order_client_id,
                    symbol=normalize_symbol(symbol),
                    side=side,
                    price=item.price,
                    quantity=item.quantity,
                    status=local_status,
                    created_at=created,
                    updated_at=updated,
                )
            )
        return orders

    def close(self) -> None:
        self.client.close()


class DryRunExchangeClient(ExchangeClient):
    """In-memory exchange adapter for dry runs and tests."""

    def __init__(
        self,
        balances: list[Balance] | None = None,
        orderbooks: dict[str, tuple[Decimal, Decimal]] | None = None,
        exchange_info: list[PairInfo] | None = None,
    ) -> None:
        self._balances = balances or [Balance(asset="TRY", free=Decimal("0"))]
        self._orderbooks = orderbooks or {}
        self._open_orders: list[Order] = []
        self._exchange_info = exchange_info or []
        self._rng = Random(42)
        self._fills: list[TradeFill] = []

    def get_ticker_stats(self) -> list[dict[str, object]]:
        stats: list[dict[str, object]] = []
        for symbol, (bid, ask) in self._orderbooks.items():
            mid = (bid + ask) / Decimal("2")
            stats.append(
                {
                    "pairSymbol": normalize_symbol(symbol),
                    "volume": "1000",
                    "last": str(mid),
                    "high": str(mid * Decimal("1.01")),
                    "low": str(mid * Decimal("0.99")),
                }
            )
        return stats

    def get_candles(self, symbol: str, limit: int) -> list[dict[str, object]]:
        bid, ask = self.get_orderbook(symbol)
        mid = (bid + ask) / Decimal("2")
        if limit <= 0:
            return []
        return [{"close": str(mid)} for _ in range(limit)]

    def get_balances(self) -> list[Balance]:
        return self._balances

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[Decimal, Decimal]:
        del limit
        if symbol not in self._orderbooks:
            raise ValueError(f"Missing orderbook for {symbol}")
        bid, ask = self._orderbooks[symbol]
        return Decimal(str(bid)), Decimal(str(ask))

    def get_exchange_info(self) -> list[PairInfo]:
        return self._exchange_info

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        del pair_symbol
        return OpenOrders(bids=[], asks=[])

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int) -> list[OrderSnapshot]:
        del pair_symbol, start_ms, end_ms
        return []

    def get_order(self, order_id: str) -> OrderSnapshot:
        for order in self._open_orders:
            if order.order_id == order_id:
                return OrderSnapshot(
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                    pair_symbol=normalize_symbol(order.symbol),
                    side=order.side,
                    price=order.price,
                    quantity=order.quantity,
                    status=ExchangeOrderStatus.OPEN,
                    timestamp=int(order.created_at.timestamp() * 1000),
                    update_time=int(order.updated_at.timestamp() * 1000),
                )
        raise ValueError(f"Order not found: {order_id}")

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> Order:
        # Dry-run path must remain Decimal-native end-to-end.
        if not isinstance(price, Decimal) or not isinstance(quantity, Decimal):
            raise TypeError("DryRunExchangeClient.place_limit_order expects Decimal price/quantity")
        if side not in {OrderSide.BUY, OrderSide.SELL}:
            raise ValueError(f"Unsupported side: {side}")

        order = Order(
            order_id=f"dry-{len(self._open_orders) + 1}",
            client_order_id=client_order_id,
            symbol=normalize_symbol(symbol),
            side=side,
            price=price,
            quantity=quantity,
            status=OrderStatus.OPEN,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._open_orders.append(order)
        self._maybe_fill(order)
        return order

    def _maybe_fill(self, order: Order) -> None:
        bid, ask = self.get_orderbook(order.symbol)
        mark = (bid + ask) / Decimal("2")
        should_fill = (order.side == OrderSide.BUY and order.price >= mark) or (
            order.side == OrderSide.SELL and order.price <= mark
        )
        if not should_fill and self._rng.random() > 0.2:
            return
        fee = order.price * order.quantity * Decimal("0.001")
        fill = TradeFill(
            fill_id=f"fill-{order.order_id}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            qty=order.quantity,
            fee=fee,
            fee_currency="TRY",
            ts=datetime.now(UTC),
        )
        self._fills.append(fill)
        self.cancel_order(order.order_id)

    def cancel_order(self, order_id: str) -> bool:
        for idx, order in enumerate(self._open_orders):
            if order.order_id == order_id:
                self._open_orders.pop(idx)
                return True
        return False

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            return list(self._open_orders)
        normalized = normalize_symbol(symbol)
        return [
            order for order in self._open_orders if normalize_symbol(order.symbol) == normalized
        ]

    def get_recent_fills(self, pair_symbol: str, since_ms: int | None = None) -> list[TradeFill]:
        normalized = normalize_symbol(pair_symbol)
        if since_ms is None:
            return [fill for fill in self._fills if normalize_symbol(fill.symbol) == normalized]
        return [
            fill
            for fill in self._fills
            if normalize_symbol(fill.symbol) == normalized
            and int(fill.ts.timestamp() * 1000) >= since_ms
        ]

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_order_id: str,
    ) -> OrderAck:
        if not isinstance(price, Decimal) or not isinstance(qty, Decimal):
            raise TypeError("DryRunExchangeClient.submit_limit_order expects Decimal price/qty")
        order = self.place_limit_order(
            symbol=symbol,
            side=OrderSide(side.lower()),
            price=price,
            quantity=qty,
            client_order_id=client_order_id,
        )
        return OrderAck(exchange_order_id=order.order_id, status="submitted", raw=None)

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool:
        return self.cancel_order(exchange_order_id)

    def cancel_order_by_client_order_id(self, client_order_id: str) -> bool:
        for order in self._open_orders:
            if order.client_order_id == client_order_id:
                return self.cancel_order(order.order_id)
        return False

    def list_open_orders_stage4(self, symbol: str | None = None) -> list[Stage4Order]:
        return [
            Stage4Order(
                symbol=order.symbol,
                side=order.side.value,
                type="limit",
                price=order.price,
                qty=order.quantity,
                status=order.status.value,
                created_at=order.created_at,
                updated_at=order.updated_at,
                exchange_order_id=order.order_id,
                client_order_id=order.client_order_id,
                mode="dry_run",
            )
            for order in self.list_open_orders(symbol)
        ]

    def close(self) -> None:
        return None


def _parse_stage4_open_order_item(
    item: dict[str, object],
    *,
    side_parser: Callable[[dict[str, object]], OrderSide | None],
    status_parser: Callable[[dict[str, object]], tuple[ExchangeOrderStatus, str]],
) -> Stage4Order | None:
    side = side_parser(item)
    if side is None:
        return None

    status, _ = status_parser(item)
    local_status = {
        ExchangeOrderStatus.OPEN: "open",
        ExchangeOrderStatus.PARTIAL: "partial",
        ExchangeOrderStatus.FILLED: "filled",
        ExchangeOrderStatus.CANCELED: "canceled",
        ExchangeOrderStatus.REJECTED: "rejected",
    }.get(status, "unknown")

    ts_ms = int(item.get("time") or item.get("timestamp") or int(time() * 1000))
    updated_ms = int(item.get("updateTime") or item.get("update_time") or ts_ms)
    raw_symbol = item.get("pairSymbolNormalized") or item.get("pairSymbol")
    if raw_symbol is None:
        return None

    return Stage4Order(
        symbol=normalize_symbol(str(raw_symbol)),
        side=side.value,
        type=str(item.get("type") or "limit").lower(),
        price=parse_decimal(item.get("price")),
        qty=parse_decimal(item.get("quantity") or item.get("amount")),
        status=local_status,
        created_at=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        updated_at=datetime.fromtimestamp(updated_ms / 1000, tz=UTC),
        exchange_order_id=(str(item.get("id")) if item.get("id") is not None else None),
        client_order_id=(str(item.get("orderClientId")) if item.get("orderClientId") else None),
        mode="live",
    )


class BtcturkHttpClientStage4(ExchangeClientStage4):
    """Decimal-native Stage 4 adapter over BtcturkHttpClient private/public requests."""

    def __init__(self, client: BtcturkHttpClient) -> None:
        self.client = client

    def _open_order_rows(self, symbol: str | None = None) -> list[dict[str, object]]:
        if symbol is None:
            raise ConfigurationError(
                "Stage4 list_open_orders requires explicit symbol to avoid openOrders fanout"
            )

        payload = self.client._private_get(
            "/api/v1/openOrders", params={"pairSymbol": _btcturk_pair_symbol(symbol)}
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("Malformed open orders payload")

        rows: list[dict[str, object]] = []
        for side_key in ("bids", "asks"):
            side_rows = data.get(side_key)
            if not isinstance(side_rows, list):
                raise ValueError("Malformed open orders bids/asks payload")
            for row in side_rows:
                if not isinstance(row, dict):
                    raise ValueError("Malformed open order item in payload")
                rows.append(row)
        return rows

    def list_open_orders(self, symbol: str | None = None) -> list[Stage4Order]:
        parsed: list[Stage4Order] = []
        for row in self._open_order_rows(symbol):
            order = _parse_stage4_open_order_item(
                row,
                side_parser=self.client._parse_side,
                status_parser=self.client._parse_exchange_status,
            )
            if order is not None:
                parsed.append(order)
        return parsed

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_order_id: str,
    ) -> OrderAck:
        return self.client.submit_limit_order(symbol, side, price, qty, client_order_id)

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool:
        return self.client.cancel_order_by_exchange_id(exchange_order_id)

    def cancel_order_by_client_order_id(self, client_order_id: str) -> bool:
        try:
            return self.client.cancel_order_by_client_order_id(client_order_id)
        except ExchangeError:
            return False

    def get_recent_fills(self, symbol: str, since_ms: int | None = None) -> list[TradeFill]:
        params: dict[str, str | int] = {"pairSymbol": _btcturk_pair_symbol(symbol)}
        if since_ms is not None:
            params["startDate"] = since_ms

        payload = self.client._private_get("/api/v1/users/transactions/trade", params=params)
        rows = self.client._extract_fill_rows(payload, path="/api/v1/users/transactions/trade")
        fills: list[TradeFill] = []
        for row in rows:
            fill_id_raw = row.get("id") or row.get("tradeId") or row.get("transactionId")
            if fill_id_raw not in (None, ""):
                fill_id = str(fill_id_raw)
            else:
                fallback_src = (
                    f"{row.get('orderId') or ''}|{row.get('timestamp') or row.get('date') or ''}|"
                    f"{row.get('price') or ''}|{row.get('amount') or row.get('quantity') or ''}"
                )
                fill_id = hashlib.sha256(fallback_src.encode("utf-8")).hexdigest()
            side = self.client._parse_side({"type": row.get("orderType") or row.get("type")})
            if side is None:
                continue
            ts_ms = int(row.get("timestamp") or row.get("date") or int(time() * 1000))
            fills.append(
                TradeFill(
                    fill_id=fill_id,
                    order_id=str(row.get("orderId") or ""),
                    symbol=symbol,
                    side=side,
                    price=parse_decimal(row.get("price")),
                    qty=parse_decimal(row.get("amount") or row.get("quantity")),
                    fee=parse_decimal(row.get("fee") or 0),
                    fee_currency=str(row.get("feeCurrency") or "TRY"),
                    ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                )
            )
        return fills

    def get_exchange_info(self) -> list[PairInfo]:
        return self.client.get_exchange_info()

    def close(self) -> None:
        self.client.close()


class DryRunExchangeClientStage4(ExchangeClientStage4):
    def __init__(self, client: DryRunExchangeClient) -> None:
        self.client = client

    def list_open_orders(self, symbol: str | None = None) -> list[Stage4Order]:
        return [
            Stage4Order(
                symbol=order.symbol,
                side=order.side.value,
                type="limit",
                price=order.price,
                qty=order.quantity,
                status=order.status.value,
                created_at=order.created_at,
                updated_at=order.updated_at,
                exchange_order_id=order.order_id,
                client_order_id=order.client_order_id,
                mode="dry_run",
            )
            for order in self.client.list_open_orders(symbol)
        ]

    def submit_limit_order(
        self, symbol: str, side: str, price: Decimal, qty: Decimal, client_order_id: str
    ) -> OrderAck:
        return self.client.submit_limit_order(symbol, side, price, qty, client_order_id)

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool:
        return self.client.cancel_order_by_exchange_id(exchange_order_id)

    def cancel_order_by_client_order_id(self, client_order_id: str) -> bool:
        return self.client.cancel_order_by_client_order_id(client_order_id)

    def get_recent_fills(self, symbol: str, since_ms: int | None = None) -> list[TradeFill]:
        return self.client.get_recent_fills(symbol, since_ms)

    def get_exchange_info(self) -> list[PairInfo]:
        return self.client.get_exchange_info()

    def close(self) -> None:
        self.client.close()
