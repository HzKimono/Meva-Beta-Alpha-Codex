from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_FIELDS = ("run_id", "cycle_id", "client_order_id", "order_id", "symbol")
_CONTEXT_VARS: dict[str, ContextVar[str | None]] = {
    field: ContextVar(field, default=None) for field in _FIELDS
}


def get_logging_context() -> dict[str, str | None]:
    context: dict[str, str | None] = {}
    for field, context_var in _CONTEXT_VARS.items():
        value = context_var.get()
        if value is not None:
            context[field] = value
    return context


@contextmanager
def with_logging_context(**context: str | None) -> Iterator[None]:
    tokens: dict[str, object] = {}
    try:
        for key, value in context.items():
            context_var = _CONTEXT_VARS.get(key)
            if context_var is None or value is None:
                continue
            tokens[key] = context_var.set(value)
        yield
    finally:
        for key, token in tokens.items():
            _CONTEXT_VARS[key].reset(token)


@contextmanager
def with_cycle_context(cycle_id: str, run_id: str | None = None) -> Iterator[None]:
    with with_logging_context(cycle_id=cycle_id, run_id=run_id):
        yield
