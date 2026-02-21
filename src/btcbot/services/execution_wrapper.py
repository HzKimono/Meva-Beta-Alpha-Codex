from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from btcbot.observability import get_instrumentation
from btcbot.services.execution_errors import ExecutionErrorCategory, classify_exchange_error
from btcbot.services.retry import retry_with_backoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UncertainResult:
    action: str
    category: ExecutionErrorCategory
    error: Exception


class _RetryableExecutionError(Exception):
    def __init__(self, *, action: str, category: ExecutionErrorCategory, error: Exception) -> None:
        super().__init__(str(error))
        self.action = action
        self.category = category
        self.error = error


class ExecutionWrapper:
    def __init__(
        self,
        exchange: Any,
        *,
        submit_retry_max_attempts: int = 2,
        cancel_retry_max_attempts: int = 2,
        retry_base_delay_ms: int = 250,
        retry_max_delay_ms: int = 4000,
        sleep_fn=None,
    ) -> None:
        self.exchange = exchange
        self.submit_retry_max_attempts = max(1, submit_retry_max_attempts)
        self.cancel_retry_max_attempts = max(1, cancel_retry_max_attempts)
        self.retry_base_delay_ms = max(0, retry_base_delay_ms)
        self.retry_max_delay_ms = max(self.retry_base_delay_ms, retry_max_delay_ms)
        self.sleep_fn = sleep_fn or time.sleep

    def submit_limit_order(self, **kwargs: Any) -> Any | UncertainResult:
        return self._execute("submit", self.submit_retry_max_attempts, self._submit_call, **kwargs)

    def cancel_order(self, **kwargs: Any) -> Any | UncertainResult:
        return self._execute("cancel", self.cancel_retry_max_attempts, self._cancel_call, **kwargs)

    def _submit_call(self, **kwargs: Any) -> Any:
        if hasattr(self.exchange, "submit_limit_order"):
            return self.exchange.submit_limit_order(**kwargs)
        return self.exchange.place_limit_order(**kwargs)

    def _cancel_call(self, **kwargs: Any) -> Any:
        if "exchange_order_id" in kwargs and hasattr(self.exchange, "cancel_order_by_exchange_id"):
            return self.exchange.cancel_order_by_exchange_id(kwargs["exchange_order_id"])
        if "order_id" in kwargs and hasattr(self.exchange, "cancel_order"):
            return self.exchange.cancel_order(kwargs["order_id"])
        raise ValueError("unsupported cancel arguments")

    def _execute(self, action: str, max_attempts: int, fn, **kwargs: Any) -> Any | UncertainResult:
        def _wrapped_call() -> Any:
            try:
                get_instrumentation().counter(
                    "execution_attempts_total",
                    1,
                    attrs={"action": action, "category": "ok"},
                )
                return fn(**kwargs)
            except Exception as exc:  # noqa: BLE001
                category = classify_exchange_error(exc)
                get_instrumentation().counter(
                    "execution_attempts_total",
                    1,
                    attrs={"action": action, "category": category.value},
                )
                if category in {ExecutionErrorCategory.RATE_LIMIT, ExecutionErrorCategory.TRANSIENT}:
                    raise _RetryableExecutionError(action=action, category=category, error=exc) from exc
                raise

        try:
            return retry_with_backoff(
                _wrapped_call,
                max_attempts=max_attempts,
                base_delay_ms=self.retry_base_delay_ms,
                max_delay_ms=self.retry_max_delay_ms,
                jitter_seed=int(hashlib.sha256(action.encode("utf-8")).hexdigest()[:8], 16),
                retry_on_exceptions=(_RetryableExecutionError,),
                retry_after_getter=self._retry_after,
                sleep_fn=self.sleep_fn,
                on_retry=lambda attempt: get_instrumentation().counter(
                    "execution_retries_total",
                    1,
                    attrs={"action": action, "category": attempt.error_type},
                ),
            )
        except _RetryableExecutionError as exc:
            raise exc.error
        except Exception as exc:  # noqa: BLE001
            category = classify_exchange_error(exc)
            if category == ExecutionErrorCategory.UNCERTAIN:
                get_instrumentation().counter("execution_uncertain_total", 1, attrs={"action": action})
                logger.warning("execution_uncertain", extra={"extra": {"action": action}})
                return UncertainResult(action=action, category=category, error=exc)
            if category == ExecutionErrorCategory.REJECT:
                get_instrumentation().counter(
                    "execution_reject_total",
                    1,
                    attrs={"action": action, "reason": type(exc).__name__},
                )
            raise

    def _retry_after(self, exc: Exception) -> str | None:
        inner = exc.error if isinstance(exc, _RetryableExecutionError) else exc
        status_error = getattr(inner, "response", None)
        if status_error is not None and getattr(status_error, "headers", None) is not None:
            value = status_error.headers.get("Retry-After")
            if value:
                return value
        for candidate in (getattr(inner, "response_body", None), str(inner)):
            if not candidate:
                continue
            match = re.search(r"retry-?after[=: ]+(\d+)", str(candidate), flags=re.IGNORECASE)
            if match is not None:
                return match.group(1)
        return None

