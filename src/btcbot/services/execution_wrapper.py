from __future__ import annotations

import hashlib
import inspect
import logging
import re
import time
from dataclasses import dataclass
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
        payload = self._normalize_submit_kwargs(kwargs)
        submit_fn = getattr(self.exchange, "submit_limit_order", None)
        place_fn = getattr(self.exchange, "place_limit_order", None)

        if callable(submit_fn) and "qty" in payload:
            return submit_fn(**self._filter_kwargs_for_callable(submit_fn, payload))
        if callable(place_fn):
            place_payload = dict(payload)
            if "quantity" not in place_payload and "qty" in place_payload:
                place_payload["quantity"] = place_payload["qty"]
            return place_fn(**self._filter_kwargs_for_callable(place_fn, place_payload))

        available = [
            name for name in ("submit_limit_order", "place_limit_order") if callable(getattr(self.exchange, name, None))
        ]
        raise RuntimeError(
            "No compatible submit method on exchange "
            f"for payload keys={sorted(payload.keys())}; available_methods={available}"
        )

    def _normalize_submit_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        payload = dict(kwargs)
        qty = payload.get("qty")
        quantity = payload.get("quantity")
        if qty is not None and quantity is not None and qty != quantity:
            raise ValueError(f"qty and quantity mismatch: qty={qty!r}, quantity={quantity!r}")
        if qty is None and quantity is not None:
            payload["qty"] = quantity
        if quantity is None and qty is not None:
            payload["quantity"] = qty

        required_base = ("symbol", "side", "price", "client_order_id")
        for field in required_base:
            if payload.get(field) in (None, ""):
                raise ValueError(f"missing required submit field: {field}")
        if payload.get("qty") in (None, "") and payload.get("quantity") in (None, ""):
            raise ValueError("missing required submit field: qty or quantity")
        return payload

    def _filter_kwargs_for_callable(self, fn: Any, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return payload
        accepts_var_kwargs = any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )
        if accepts_var_kwargs:
            return payload
        allowed = set(signature.parameters.keys())
        return {key: value for key, value in payload.items() if key in allowed}

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
                if category in {
                    ExecutionErrorCategory.RATE_LIMIT,
                    ExecutionErrorCategory.TRANSIENT,
                }:
                    raise _RetryableExecutionError(
                        action=action, category=category, error=exc
                    ) from exc
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
            underlying_error = exc.error
            category = classify_exchange_error(underlying_error)
            if category == ExecutionErrorCategory.UNCERTAIN:
                get_instrumentation().counter(
                    "execution_uncertain_total", 1, attrs={"action": action}
                )
                logger.warning("execution_uncertain", extra={"extra": {"action": action}})
                return UncertainResult(action=action, category=category, error=underlying_error)
            if category == ExecutionErrorCategory.REJECT:
                get_instrumentation().counter("execution_reject_total", 1, attrs={"action": action})
                raise underlying_error from exc
            if category in {ExecutionErrorCategory.RATE_LIMIT, ExecutionErrorCategory.TRANSIENT}:
                get_instrumentation().counter(
                    "execution_retry_exhausted_total", 1, attrs={"action": action}
                )
                if action == "cancel":
                    logger.warning("execution_uncertain", extra={"extra": {"action": action}})
                    return UncertainResult(
                        action=action,
                        category=ExecutionErrorCategory.UNCERTAIN,
                        error=underlying_error,
                    )
                raise underlying_error from exc
            raise underlying_error from exc
        except Exception as exc:  # noqa: BLE001
            category = classify_exchange_error(exc)
            if category == ExecutionErrorCategory.UNCERTAIN:
                get_instrumentation().counter(
                    "execution_uncertain_total", 1, attrs={"action": action}
                )
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
