from __future__ import annotations

import httpx

from btcbot.domain.models import ExchangeError
from btcbot.services.execution_errors import ExecutionErrorCategory, classify_exchange_error


def test_classify_exchange_error_maps_exchange_error_429_to_rate_limit() -> None:
    err = ExchangeError("rate-limited", status_code=429)

    assert classify_exchange_error(err) == ExecutionErrorCategory.RATE_LIMIT


def test_classify_exchange_error_maps_http_status_error_429_to_rate_limit() -> None:
    request = httpx.Request("GET", "https://example.test/public")
    response = httpx.Response(429, request=request)
    err = httpx.HTTPStatusError("too many requests", request=request, response=response)

    assert classify_exchange_error(err) == ExecutionErrorCategory.RATE_LIMIT


def test_classify_exchange_error_maps_breaker_open_shape_to_rate_limit() -> None:
    err = ExchangeError("rate limit breaker open", error_message="breaker_open")

    assert classify_exchange_error(err) == ExecutionErrorCategory.RATE_LIMIT
