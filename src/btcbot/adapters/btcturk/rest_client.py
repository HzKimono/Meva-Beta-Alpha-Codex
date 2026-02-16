from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

from btcbot.adapters.btcturk.clock_sync import ClockSyncService
from btcbot.adapters.btcturk.instrumentation import MetricsSink
from btcbot.adapters.btcturk.rate_limit import AsyncTokenBucket
from btcbot.adapters.btcturk.retry import RetryDecision, async_retry, compute_delay
from btcbot.domain.models import ExchangeError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestReliabilityConfig:
    timeout_seconds: float = 10.0
    max_attempts: int = 4
    base_delay_seconds: float = 0.4
    max_delay_seconds: float = 4.0


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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock_sync = clock_sync
        self.limiter = limiter
        self.metrics = metrics
        self.reliability = reliability or RestReliabilityConfig()
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=self.reliability.timeout_seconds,
        )

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
            headers: dict[str, str] = {"X-Correlation-ID": correlation_id or "n/a"}
            if is_private:
                headers.update(self._auth_headers())
            started = monotonic()
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=headers,
            )
            latency_ms = (monotonic() - started) * 1000
            self.metrics.observe_ms(f"rest_{method.lower()}_latency", latency_ms)
            if response.status_code == 429:
                self.metrics.inc("429_count")
            if response.status_code >= 400:
                raise self._to_exchange_error(response, method=method, path=path)
            payload = response.json()
            if not isinstance(payload, dict):
                raise ExchangeError(
                    "BTCTurk payload must be dict",
                    status_code=response.status_code,
                )
            return payload

        def _classify(exc: Exception) -> RetryDecision:
            if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
                delay = compute_delay(
                    attempt=1,
                    base_delay_seconds=self.reliability.base_delay_seconds,
                    max_delay_seconds=self.reliability.max_delay_seconds,
                    retry_after_header=None,
                    jitter_seed=17,
                )
                self.metrics.inc("rest_retries")
                return RetryDecision(retry=True, delay_seconds=delay)
            if isinstance(exc, ExchangeError):
                status = exc.status_code or 0
                if status == 429 or status >= 500:
                    retry_after = None
                    if exc.response_body:
                        try:
                            retry_after = json.loads(exc.response_body).get("retry_after")
                        except Exception:  # noqa: BLE001
                            retry_after = None
                    delay = compute_delay(
                        attempt=1,
                        base_delay_seconds=self.reliability.base_delay_seconds,
                        max_delay_seconds=self.reliability.max_delay_seconds,
                        retry_after_header=retry_after,
                        jitter_seed=17,
                    )
                    self.metrics.inc("rest_retries")
                    return RetryDecision(retry=True, delay_seconds=delay)
            return RetryDecision(retry=False, delay_seconds=0)

        return await async_retry(
            _call,
            max_attempts=self.reliability.max_attempts,
            classify=_classify,
        )

    def _auth_headers(self) -> dict[str, str]:
        stamp = str(self.clock_sync.stamped_now_ms())
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            f"{self.api_key}{stamp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return {"X-PCK": self.api_key, "X-Stamp": stamp, "X-Signature": signature}

    def _to_exchange_error(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
    ) -> ExchangeError:
        body = response.text[:300]
        return ExchangeError(
            f"BTCTurk REST error status={response.status_code}",
            status_code=response.status_code,
            request_method=method,
            request_path=path,
            response_body=body,
        )
