from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_RUN_ID: ContextVar[str] = ContextVar("run_id", default="")
_CYCLE_ID: ContextVar[str] = ContextVar("cycle_id", default="")


def get_logging_context() -> dict[str, str]:
    return {
        "run_id": _RUN_ID.get(),
        "cycle_id": _CYCLE_ID.get(),
    }


@contextmanager
def with_cycle_context(cycle_id: str, run_id: str) -> Iterator[None]:
    run_token = _RUN_ID.set(run_id)
    cycle_token = _CYCLE_ID.set(cycle_id)
    try:
        yield
    finally:
        _RUN_ID.reset(run_token)
        _CYCLE_ID.reset(cycle_token)
