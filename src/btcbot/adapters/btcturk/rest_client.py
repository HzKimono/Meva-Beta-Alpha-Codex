from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any

import httpx

from btcbot.adapters.btcturk.clock_sync import ClockSyncService
from btcbot.adapters.btcturk.instrumentation import MetricsSink
from btcbot.adapters.btcturk.rate_limit import AsyncTokenBucket
from btcbot.adapters.btcturk.retry import RetryDecision, async_retry, compute_delay
from btcbot.domain.models import ExchangeError
from btcbot.observability import get_instrumentation

logger = logging.getLogger(__name__)


class RestErrorKind(StrEnum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    CLIENT = "client"
    EXCHANGE = "exchange"


class RestRequestError(RuntimeError):
    def __init__(
        self,
        *,
        kind: RestErrorKind,
        message: str,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.payload = payload


@dataclass(frozen=True)
class RestReliabilityConfig:
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    write_timeout_seconds: float = 10.0
    pool_timeout_seconds: float = 5.0
    max_attempts: int = 4
    base_delay_seconds: float = 0.4
    max_delay_seconds: float = 4.0


@dataclass(frozen=True)
class OrderOperationPolicy:
    """Idempotent-safe retry semantics for order operations.

    - submit: when transport/5xx/429 failures occur after send, do not blindly resubmit;
      first check order existence by client_order_id to avoid duplicates.
    - cancel: for not-found/already-canceled style responses, reconcile and treat as success
      if the order is terminal or absent from open orders.
    """

    safe_submit_retries: bool = True
    safe_cancel_retries: bool = True


class BtcturkRestClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str,
        clock_sync: ClockSyncService,
        limiter: AsyncTokenBucket,
        metrics: MetricsSink,
        reliability: RestReliabilityConfig | None = None,
        operation_policy: OrderOperationPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock_sync = clock_sync
        self.limiter = limiter
        self.metrics = metrics
        self.reliability = reliability or RestReliabilityConfig()
        self.operation_policy = operation_policy or OrderOperationPolicy()
        timeout = httpx.Timeout(
            connect=self.reliability.connect_timeout_seconds,
            read=self.reliability.read_timeout_seconds,
            write=self.reliability.write_timeout_seconds,
            pool=self.reliability.pool_timeout_seconds,
        )
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        is_private: bool,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        await self.clock_sync.maybe_sync()

        async def _call() -> dict[str, Any]:
            await self.limiter.acquire()
            headers = {"X-Correlation-ID": correlation_id or "n/a"}
            if is_private:
                headers.update(self._auth_headers())
            started = monotonic()
            try:
                with get_instrumentation().trace(
                    "rest_call", attrs={"method": method, "path": path}
                ):
                    response = await self._client.request(
                        method,
                        path,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise RestRequestError(kind=RestErrorKind.NETWORK, message=str(exc)) from exc

            self.metrics.observe_ms(
                f"rest_{method.lower()}_latency",
                (monotonic() - started) * 1000,
            )
            if response.status_code == 429:
                self.metrics.inc("429_count")
                self.metrics.inc("rest_429_rate")

            if response.status_code >= 400:
                self._raise_http_error(response)

            payload = response.json()
            if not isinstance(payload, dict):
                raise RestRequestError(
                    kind=RestErrorKind.EXCHANGE,
                    message="BTCTurk payload must be dict",
                    status_code=response.status_code,
                    headers=dict(response.headers),
                )

            if payload.get("success") is False:
                raise RestRequestError(
                    kind=RestErrorKind.EXCHANGE,
                    message=str(payload.get("message") or "exchange error"),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    payload=payload,
                )
            return payload

        def _classify(exc: Exception, attempt: int) -> RetryDecision:
            if not isinstance(exc, RestRequestError):
                return RetryDecision(retry=False, delay_seconds=0)

            retryable = exc.kind in {
                RestErrorKind.NETWORK,
                RestErrorKind.SERVER,
                RestErrorKind.RATE_LIMIT,
            }
            if not retryable:
                return RetryDecision(retry=False, delay_seconds=0)

            retry_after = None
            if exc.headers:
                retry_after = exc.headers.get("retry-after")
            delay = compute_delay(
                attempt=attempt,
                base_delay_seconds=self.reliability.base_delay_seconds,
                max_delay_seconds=self.reliability.max_delay_seconds,
                retry_after_header=retry_after,
                jitter_seed=17,
            )
            self.metrics.inc("rest_retries")
            self.metrics.inc("rest_retry_rate")
            return RetryDecision(retry=True, delay_seconds=delay)

        try:
            return await async_retry(
                _call,
                max_attempts=self.reliability.max_attempts,
                classify=_classify,
            )
        except RestRequestError as exc:
            raise self._to_exchange_error(exc, method=method, path=path) from exc

    async def submit_order_safe(
        self,
        *,
        payload: dict[str, Any],
        client_order_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        try:
            return await self.request(
                "POST",
                "/api/v1/order",
                is_private=True,
                json_body=payload,
                correlation_id=correlation_id,
            )
        except ExchangeError as exc:
            if not self.operation_policy.safe_submit_retries:
                raise
            if (exc.status_code or 0) in {429, 500, 502, 503, 504}:
                existing = await self.find_open_order_by_client_order_id(client_order_id)
                if existing is not None:
                    return {"success": True, "data": existing, "idempotent": True}
            raise

    async def cancel_order_safe(
        self,
        *,
        order_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        try:
            return await self.request(
                "DELETE",
                f"/api/v1/order/{order_id}",
                is_private=True,
                correlation_id=correlation_id,
            )
        except ExchangeError as exc:
            if not self.operation_policy.safe_cancel_retries:
                raise
            if (exc.status_code or 0) in {400, 404, 409}:
                is_open = await self.is_order_open(order_id)
                if not is_open:
                    return {"success": True, "idempotent": True, "order_id": order_id}
            raise

    async def find_open_order_by_client_order_id(
        self, client_order_id: str
    ) -> dict[str, object] | None:
        payload = await self.request("GET", "/api/v1/openOrders", is_private=True)
        for side_key in ("bids", "asks"):
            data = payload.get("data")
            side_rows = data.get(side_key, []) if isinstance(data, dict) else []
            if not isinstance(side_rows, list):
                continue
            for row in side_rows:
                if isinstance(row, dict) and row.get("orderClientId") == client_order_id:
                    return row
        return None

    async def is_order_open(self, order_id: str) -> bool:
        payload = await self.request("GET", "/api/v1/openOrders", is_private=True)
        data = payload.get("data")
        if not isinstance(data, dict):
            return False
        for side_key in ("bids", "asks"):
            side_rows = data.get(side_key, [])
            if not isinstance(side_rows, list):
                continue
            for row in side_rows:
                if isinstance(row, dict) and str(row.get("id")) == str(order_id):
                    return True
        return False

    def _auth_headers(self) -> dict[str, str]:
        stamp = str(self.clock_sync.stamped_now_ms())
        secret_key = base64.b64decode(self.api_secret)
        signature = hmac.new(
            secret_key,
            f"{self.api_key}{stamp}".encode(),
            hashlib.sha256,
        ).digest()
        return {
            "X-PCK": self.api_key,
            "X-Stamp": stamp,
            "X-Signature": base64.b64encode(signature).decode(),
        }

    def _raise_http_error(self, response: httpx.Response) -> None:
        kind = RestErrorKind.CLIENT
        if response.status_code == 429:
            kind = RestErrorKind.RATE_LIMIT
        elif response.status_code >= 500:
            kind = RestErrorKind.SERVER
        message = response.text[:300]
        payload: dict[str, object] | None = None
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:  # noqa: BLE001
            payload = None

        raise RestRequestError(
            kind=kind,
            message=message,
            status_code=response.status_code,
            headers=dict(response.headers),
            payload=payload,
        )

    def _to_exchange_error(self, exc: RestRequestError, *, method: str, path: str) -> ExchangeError:
        return ExchangeError(
            f"BTCTurk REST error kind={exc.kind.value}",
            status_code=exc.status_code,
            error_code=exc.payload.get("code") if isinstance(exc.payload, dict) else None,
            error_message=exc.payload.get("message") if isinstance(exc.payload, dict) else str(exc),
            request_method=method,
            request_path=path,
            response_body=str(exc),
        )
