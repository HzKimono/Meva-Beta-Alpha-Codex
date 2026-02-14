from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_RUN_ID: ContextVar[str | None] = ContextVar("run_id", default=None)
_CYCLE_ID: ContextVar[str | None] = ContextVar("cycle_id", default=None)


def get_logging_context() -> dict[str, str | None]:
    context: dict[str, str | None] = {}
    run_id = _RUN_ID.get()
    cycle_id = _CYCLE_ID.get()
    if run_id is not None:
        context["run_id"] = run_id
    if cycle_id is not None:
        context["cycle_id"] = cycle_id
    return context


@contextmanager
def with_cycle_context(cycle_id: str, run_id: str | None = None) -> Iterator[None]:
    run_token = _RUN_ID.set(run_id) if run_id is not None else None
    cycle_token = _CYCLE_ID.set(cycle_id)
    try:
        yield
    finally:
        if run_token is not None:
            _RUN_ID.reset(run_token)
        _CYCLE_ID.reset(cycle_token)
