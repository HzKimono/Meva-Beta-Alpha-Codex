from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"
SENSITIVE_KEYWORDS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "auth",
    "private_key",
    "access_key",
    "signature",
)

_PLAIN_SECRET_PATTERNS = (
    re.compile(r"(?i)(btcturk_api_key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(btcturk_api_secret\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(x-signature\s*[=:]\s*)([^\s,;]+)"),
)


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key).lower()
    return any(word in lowered for word in SENSITIVE_KEYWORDS)


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[str(key)] = REDACTED
            else:
                sanitized[str(key)] = redact_value(item)
        return sanitized
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _PLAIN_SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", redacted)
    return redacted
