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
    re.compile(r"(?i)(authorization\s*[=:]\s*)(bearer\s+)?([^\s,;]+)"),
    re.compile(r"(?i)(x-api-key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(x-pck\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(x-stamp\s*[=:]\s*)([^\s,;]+)"),
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


def _redact_match(match: re.Match[str]) -> str:
    prefix = match.group(1)
    optional_scheme = ""
    if match.lastindex and match.lastindex >= 3:
        optional_scheme = match.group(2) or ""
    return f"{prefix}{optional_scheme}{REDACTED}"


def redact_text(value: str) -> str:
    redacted = value

    for pattern in _PLAIN_SECRET_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)

    return redacted
