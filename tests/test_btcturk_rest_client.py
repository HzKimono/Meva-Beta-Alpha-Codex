from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from typing import Any

import httpx
import pytest

from btcbot.adapters.btcturk.clock_sync import ClockSyncService
from btcbot.adapters.btcturk.instrumentation import InMemoryMetricsSink
from btcbot.adapters.btcturk.rate_limit import AsyncTokenBucket
from btcbot.adapters.btcturk.rest_client import BtcturkRestClient, RestReliabilityConfig
from btcbot.domain.models import ExchangeError


class _Provider:
    async def fetch_server_time_ms(self) -> int:
        return 1_700_000_000_000


def _make_client(transport: httpx.MockTransport) -> BtcturkRestClient:
    clock = ClockSyncService(provider=_Provider())
    clock.utc_now_ms = lambda: 1_700_000_000_000  # type: ignore[method-assign]
    async_client = httpx.AsyncClient(base_url="https://api.btcturk.com", transport=transport)
    return BtcturkRestClient(
        api_key="api-key",
        api_secret=base64.b64encode(b"secret-bytes").decode(),
        base_url="https://api.btcturk.com",
        clock_sync=clock,
        limiter=AsyncTokenBucket(rate_per_sec=1_000, burst=1_000),
        metrics=InMemoryMetricsSink(),
        client=async_client,
        reliability=RestReliabilityConfig(
            max_attempts=3,
            base_delay_seconds=0.1,
            max_delay_seconds=5,
        ),
    )


def test_auth_signature_uses_base64_decoded_secret() -> None:
    client = _make_client(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"success": True}))
    )
    headers = client._auth_headers()
    expected_sig = base64.b64encode(
        hmac.new(
            b"secret-bytes",
            f"api-key{headers['X-Stamp']}".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert headers["X-Signature"] == expected_sig


def test_429_retry_after_header_respected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="rate")
        return httpx.Response(200, json={"success": True, "data": {"ok": True}})

    client = _make_client(httpx.MockTransport(handler))

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("btcbot.adapters.btcturk.retry.asyncio.sleep", fake_sleep)
    payload = asyncio.run(client.request("GET", "/x", is_private=False))

    assert payload["success"] is True
    assert calls["count"] == 2
    assert sleeps and sleeps[0] >= 1.0


def test_submit_safe_detects_existing_order_after_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/order":
            return httpx.Response(503, text="unavailable")
        if request.url.path == "/api/v1/openOrders":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"bids": [{"orderClientId": "cid-1", "id": "abc"}], "asks": []},
                },
            )
        return httpx.Response(404, text="missing")

    client = _make_client(httpx.MockTransport(handler))
    result = asyncio.run(
        client.submit_order_safe(
            payload={"pairSymbol": "BTCTRY"},
            client_order_id="cid-1",
            correlation_id="corr-1",
        )
    )
    assert result["idempotent"] is True


def test_cancel_safe_treats_not_found_as_success_when_not_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(404, text="not found")
        if request.url.path == "/api/v1/openOrders":
            return httpx.Response(200, json={"success": True, "data": {"bids": [], "asks": []}})
        return httpx.Response(404)

    client = _make_client(httpx.MockTransport(handler))
    result = asyncio.run(client.cancel_order_safe(order_id="ord-1", correlation_id="corr-2"))
    assert result["success"] is True


def test_429_metric_uses_effective_process_role(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def _capture(name: str, labels: dict[str, str], delta: int = 1) -> None:
        del delta
        if name == "bot_api_errors_total":
            captured.append(dict(labels))

    monkeypatch.setattr("btcbot.adapters.btcturk.rest_client.inc_counter", _capture)

    client = _make_client(
        httpx.MockTransport(lambda request: httpx.Response(429, text="rate limited"))
    )
    client.process_role = "MONITOR"

    with pytest.raises(ExchangeError):
        asyncio.run(client.request("GET", "/x", is_private=False))

    assert captured
    assert captured[0]["process_role"] == "MONITOR"
