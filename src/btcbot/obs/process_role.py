from __future__ import annotations

import os
from enum import Enum


class ProcessRole(str, Enum):
    LIVE = "LIVE"
    MONITOR = "MONITOR"


def coerce_process_role(value: str | ProcessRole | None) -> ProcessRole:
    if isinstance(value, ProcessRole):
        return value
    if value is None:
        return ProcessRole.MONITOR
    normalized = str(value).strip().upper()
    if normalized == ProcessRole.LIVE.value:
        return ProcessRole.LIVE
    if normalized == ProcessRole.MONITOR.value:
        return ProcessRole.MONITOR
    return ProcessRole.MONITOR


def get_process_role_from_env() -> ProcessRole:
    return coerce_process_role(os.getenv("APP_ROLE") or os.getenv("PROCESS_ROLE"))
