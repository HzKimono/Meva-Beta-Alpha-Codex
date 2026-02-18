from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "***REDACTED***"

SENSITIVE_KEYS = {
    "API_KEY",
    "API_SECRET",
    "SECRET",
    "SIGNATURE",
    "AUTHORIZATION",
    "TOKEN",
    "PASSWORD",
    "PASSPHRASE",
    "BTCTURK_API_KEY",
    "BTCTURK_API_SECRET",
    "X_API_KEY",
    "X_PCK",
    "X_SIGNATURE",
    "X_STAMP",
}

_SENSITIVE_PARTS = tuple(part.casefold() for part in SENSITIVE_KEYS)

_PLAIN_SECRET_PATTERNS = (
    re.compile(r"(?im)(authorization\s*[:=]\s*)(bearer\s+)?([^\s,;]+)"),
    re.compile(r"(?im)(x-api-key\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(x-pck\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(x-signature\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(x-stamp\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(btcturk_api_key\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(btcturk_api_secret\s*[:=]\s*)([^\s,;]+)"),
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).replace("-", "_").casefold()
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _mask_secret(value: str) -> str:
    if len(value) <= 4:
        return REDACTED
    return f"{value[:2]}...{value[-4:]}"


def redact_value(value: str) -> str:
    return _mask_secret(value)


def sanitize_mapping(d: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in d.items():
        key_str = str(key)
        if _is_sensitive_key(key_str):
            sanitized[key_str] = _mask_secret(str(value)) if value is not None else REDACTED
            continue

        if isinstance(value, Mapping):
            sanitized[key_str] = sanitize_mapping(value)
        elif isinstance(value, list):
            sanitized[key_str] = [
                sanitize_mapping(item) if isinstance(item, Mapping) else item for item in value
            ]
        elif isinstance(value, tuple):
            sanitized[key_str] = tuple(
                sanitize_mapping(item) if isinstance(item, Mapping) else item for item in value
            )
        else:
            sanitized[key_str] = value
    return sanitized


def _redact_match(match: re.Match[str]) -> str:
    prefix = match.group(1)
    optional_scheme = ""
    if match.lastindex and match.lastindex >= 3:
        optional_scheme = match.group(2) or ""
    return f"{prefix}{optional_scheme}[REDACTED]"


def sanitize_text(text: str, known_secrets: Iterable[str] = ()) -> str:
    redacted = text
    for secret in known_secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")

    for pattern in _PLAIN_SECRET_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)

    return redacted


def safe_repr(obj: object, *, known_secrets: Iterable[str] = ()) -> str:
    if isinstance(obj, Mapping):
        return repr(sanitize_mapping(obj))
    return sanitize_text(repr(obj), known_secrets=known_secrets)


def redact_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return sanitize_mapping(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def redact_text(value: str) -> str:
    return sanitize_text(value)
