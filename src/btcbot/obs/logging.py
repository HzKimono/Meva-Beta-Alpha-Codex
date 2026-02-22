from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from btcbot.obs.process_role import ProcessRole, coerce_process_role, get_process_role_from_env

_RUN_ID = str(uuid.uuid4())
_CYCLE_ID: ContextVar[str | None] = ContextVar("cycle_id", default=None)
_MODE_BASE: ContextVar[str | None] = ContextVar("mode_base", default=None)
_MODE_FINAL: ContextVar[str | None] = ContextVar("mode_final", default=None)
_PROCESS_ROLE: ContextVar[str] = ContextVar("process_role", default=get_process_role_from_env().value)
_STATE_DB_PATH: ContextVar[str | None] = ContextVar("state_db_path", default=None)


def _db_path_hash() -> str:
    db_path = _STATE_DB_PATH.get() or os.getenv("STATE_DB_PATH", "")
    return hashlib.sha256(db_path.encode("utf-8")).hexdigest()[:12]


class _ContextAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, object]) -> tuple[str, dict[str, object]]:
        extra = kwargs.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            kwargs["extra"] = extra
        base_extra = extra.get("extra")
        if not isinstance(base_extra, dict):
            base_extra = {}
            extra["extra"] = base_extra
        base_extra.update(
            {
                "process_role": _PROCESS_ROLE.get(),
                "run_id": _RUN_ID,
                "cycle_id": _CYCLE_ID.get(),
                "mode_base": _MODE_BASE.get(),
                "mode_final": _MODE_FINAL.get(),
                "state_db_path_hash": _db_path_hash(),
            }
        )
        return msg, kwargs


def get_logger(component: str) -> logging.LoggerAdapter:
    return _ContextAdapter(logging.getLogger(component), extra={})


def set_base_context(*, process_role: str | ProcessRole | None = None, state_db_path: str | None = None) -> None:
    if process_role is not None:
        _PROCESS_ROLE.set(coerce_process_role(process_role).value)
    if state_db_path is not None:
        _STATE_DB_PATH.set(state_db_path)


@contextmanager
def cycle_context(
    *,
    process_role: str,
    cycle_id: str,
    mode_base: str | None = None,
    mode_final: str | None = None,
) -> Iterator[None]:
    proc_tok = _PROCESS_ROLE.set(coerce_process_role(process_role).value)
    cycle_tok = _CYCLE_ID.set(cycle_id)
    base_tok = _MODE_BASE.set(mode_base)
    final_tok = _MODE_FINAL.set(mode_final)
    try:
        yield
    finally:
        _PROCESS_ROLE.reset(proc_tok)
        _CYCLE_ID.reset(cycle_tok)
        _MODE_BASE.reset(base_tok)
        _MODE_FINAL.reset(final_tok)
