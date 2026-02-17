from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from btcbot.logging_context import get_logging_context
from btcbot.security.redaction import redact_value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = getattr(record, "extra", None)
        if isinstance(extras, dict):
            payload.update(extras)

        context = get_logging_context()
        for field in ("run_id", "cycle_id", "client_order_id", "order_id", "symbol"):
            payload[field] = context.get(field)

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            payload["error_type"] = exc_type.__name__ if exc_type else "Exception"
            payload["error_message"] = str(exc_value) if exc_value is not None else ""
            payload["traceback"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["traceback"] = record.exc_text

        return json.dumps(redact_value(payload), default=str)


def _resolve_log_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level

    candidate = level if level is not None else os.getenv("LOG_LEVEL", "INFO")
    resolved = logging.getLevelName(str(candidate).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def _resolve_named_level_from_env(env_name: str, default_level: int) -> int:
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default_level
    resolved = logging.getLevelName(raw.strip().upper())
    return resolved if isinstance(resolved, int) else default_level


def setup_logging(level: str | int | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    resolved_level = _resolve_log_level(level)
    root.setLevel(resolved_level)

    if resolved_level <= logging.DEBUG:
        default_httpx_level = logging.DEBUG
        default_httpcore_level = logging.DEBUG
    else:
        default_httpx_level = logging.INFO
        default_httpcore_level = logging.WARNING

    httpx_level = _resolve_named_level_from_env("HTTPX_LOG_LEVEL", default_httpx_level)
    httpcore_level = _resolve_named_level_from_env("HTTPCORE_LOG_LEVEL", default_httpcore_level)

    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpcore_level)
