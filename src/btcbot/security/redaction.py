from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "***REDACTED***"
_TEXT_REDACTED = "[REDACTED]"

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

_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "api_secret",
    "passphrase",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "signature",
    "authorization",
    "auth",
}
_SENSITIVE_COMPACT_KEYS = {part.replace("_", "") for part in _SENSITIVE_EXACT_KEYS}
_SENSITIVE_PARTS = tuple(part.casefold() for part in SENSITIVE_KEYS)
_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "api_secret",
    "passphrase",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "signature",
    "authorization",
    "auth",
}
_SENSITIVE_EXACT_COMPACT_KEYS = {k.replace("_", "") for k in _SENSITIVE_EXACT_KEYS}

_PLAIN_SECRET_PATTERNS = (
    re.compile(r"(?im)(authorization\s*[:=]\s*)(bearer\s+)?([^\s,;]+)"),
    re.compile(r"(?im)(x-api-key\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(x-pck\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(x-signature\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(x-stamp\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(btcturk_api_key\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?im)(btcturk_api_secret\s*[:=]\s*)([^\s,;]+)"),
)

_QUERY_PARAM_PATTERN = re.compile(r"(?i)([?&]|\b)(apiKey|signature|token)=([^&\s]+)")
_JSON_KEY_VALUE_PATTERN = re.compile(
    r'("(?:api_key|apiKey|secret|api_secret|passphrase|password|token|access_token|refresh_token|signature|authorization|auth)"\s*:\s*")([^"\\]*)(")',
    re.IGNORECASE,
)


def _safe_to_str(value: object) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return _TEXT_REDACTED


def _is_sensitive_key(key: object) -> bool:
    normalized = _safe_to_str(key).replace("-", "_").casefold()
    compact = normalized.replace("_", "")
    return compact in _SENSITIVE_COMPACT_KEYS or any(part in normalized for part in _SENSITIVE_PARTS)


def redact_value(value: str) -> str:
    _ = value
    return REDACTED


def _redact_text_match(match: re.Match[str]) -> str:
    prefix = match.group(1)
    optional_scheme = ""
    if match.lastindex and match.lastindex >= 3:
        optional_scheme = match.group(2) or ""
    return f"{prefix}{optional_scheme}{_TEXT_REDACTED}"


def sanitize_text(text: str, known_secrets: list[str] | None = None) -> str:
    try:
        redacted = _safe_to_str(text)

        for secret in known_secrets or []:
            if secret:
                redacted = redacted.replace(secret, _TEXT_REDACTED)

        for pattern in _PLAIN_SECRET_PATTERNS:
            redacted = pattern.sub(_redact_text_match, redacted)

        redacted = _QUERY_PARAM_PATTERN.sub(
            lambda m: f"{m.group(1)}{m.group(2)}={_TEXT_REDACTED}",
            redacted,
        )
        redacted = _JSON_KEY_VALUE_PATTERN.sub(
            lambda m: f"{m.group(1)}{_TEXT_REDACTED}{m.group(3)}",
            redacted,
        )
        return redacted
    except Exception:  # noqa: BLE001
        return _TEXT_REDACTED


def redact_text(value: str) -> str:
    return sanitize_text(value)


def sanitize_mapping(obj: Mapping[str, Any]) -> dict[str, Any]:
    try:
        sanitized: dict[str, Any] = {}
        for key, value in obj.items():
            key_str = _safe_to_str(key)
            if _is_sensitive_key(key_str):
                sanitized[key_str] = REDACTED
                continue
            sanitized[key_str] = redact_data(value)
        return sanitized
    except Exception:  # noqa: BLE001
        return {}


def safe_repr(obj: object, *, known_secrets: list[str] | None = None) -> str:
    try:
        if isinstance(obj, Mapping):
            return repr(sanitize_mapping(obj))
        return sanitize_text(repr(obj), known_secrets=known_secrets)
    except Exception:  # noqa: BLE001
        return _TEXT_REDACTED


def redact_data(payload: Any) -> Any:
    try:
        if isinstance(payload, Mapping):
            return sanitize_mapping(payload)
        if isinstance(payload, list):
            return [redact_data(item) for item in payload]
        if isinstance(payload, tuple):
            return tuple(redact_data(item) for item in payload)
        if isinstance(payload, str):
            return sanitize_text(payload)
        return payload
    except Exception:  # noqa: BLE001
        return REDACTED
