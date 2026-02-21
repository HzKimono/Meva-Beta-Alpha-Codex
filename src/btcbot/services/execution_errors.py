from __future__ import annotations

from enum import Enum

import httpx

from btcbot.domain.models import ExchangeError


class ExecutionErrorCategory(str, Enum):
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    AUTH = "auth"
    REJECT = "reject"
    UNCERTAIN = "uncertain"
    FATAL = "fatal"


def classify_exchange_error(exc: Exception) -> ExecutionErrorCategory:
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.TimeoutException):
        return ExecutionErrorCategory.UNCERTAIN
    if isinstance(exc, httpx.ConnectError | httpx.NetworkError):
        return ExecutionErrorCategory.TRANSIENT
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status = int(exc.response.status_code)
    elif isinstance(exc, ExchangeError):
        status = int(exc.status_code) if exc.status_code is not None else None
    else:
        status = None

    if status == 429:
        return ExecutionErrorCategory.RATE_LIMIT
    if status in {401, 403}:
        return ExecutionErrorCategory.AUTH
    if status is not None and status >= 500:
        return ExecutionErrorCategory.TRANSIENT
    if status in {400, 404, 409, 422}:
        return ExecutionErrorCategory.REJECT
    if isinstance(exc, httpx.TransportError):
        return ExecutionErrorCategory.TRANSIENT
    if isinstance(exc, TimeoutError):
        return ExecutionErrorCategory.UNCERTAIN
    return ExecutionErrorCategory.FATAL

