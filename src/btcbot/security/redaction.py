from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode

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

_QUERY_PARAM_PATTERN = re.compile(r"([?&]?)(apiKey|signature|token)=([^&\s]+)", re.IGNORECASE)
_JSON_KEY_VALUE_PATTERN = re.compile(
    r'("(?:api_key|apiKey|secret|api_secret|passphrase|password|token|access_token|refresh_token|signature|authorization|auth)"\s*:\s*")([^"\\]*)(")',
    re.IGNORECASE,
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).replace("-", "_").casefold()
    compact = normalized.replace("_", "")
    return compact in _SENSITIVE_EXACT_COMPACT_KEYS or any(
        part in normalized for part in _SENSITIVE_PARTS
    )


def _mask_secret(value: str) -> str:
    if not value:
        return REDACTED
    if len(value) > 8:
        return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
    if len(value) <= 2:
        return "*" * len(value)
    return f"{'*' * (len(value) - 2)}{value[-2:]}"


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
            sanitized[key_str] = [redact_data(item) for item in value]
        elif isinstance(value, tuple):
            sanitized[key_str] = tuple(redact_data(item) for item in value)
        else:
            sanitized[key_str] = redact_data(value)
    return sanitized


def _redact_match(match: re.Match[str]) -> str:
    prefix = match.group(1)
    optional_scheme = ""
    if match.lastindex and match.lastindex >= 3:
        optional_scheme = match.group(2) or ""
    return f"{prefix}{optional_scheme}[REDACTED]"


def _sanitize_query_string(query: str) -> str:
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return query
    sanitized_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        if _is_sensitive_key(key):
            sanitized_pairs.append((key, _mask_secret(value)))
        else:
            sanitized_pairs.append((key, value))
    return urlencode(sanitized_pairs)


def sanitize_text(text: str, known_secrets: Iterable[str] = ()) -> str:
    try:
        redacted = str(text)
        for secret in known_secrets:
            if secret:
                redacted = redacted.replace(secret, _mask_secret(str(secret)))

        for pattern in _PLAIN_SECRET_PATTERNS:
            redacted = pattern.sub(_redact_match, redacted)

        redacted = _QUERY_PARAM_PATTERN.sub(
            lambda m: f"{m.group(1)}{m.group(2)}={_mask_secret(m.group(3))}", redacted
        )
        redacted = _JSON_KEY_VALUE_PATTERN.sub(
            lambda m: f"{m.group(1)}{_mask_secret(m.group(2))}{m.group(3)}", redacted
        )

        if "=" in redacted and "&" in redacted and " " not in redacted:
            redacted = _sanitize_query_string(redacted)
        return redacted
    except Exception:  # noqa: BLE001
        return REDACTED


def safe_repr(obj: object, *, known_secrets: Iterable[str] = ()) -> str:
    if isinstance(obj, Mapping):
        return repr(sanitize_mapping(obj))
    return sanitize_text(repr(obj), known_secrets=known_secrets)


def redact_data(value: Any) -> Any:
    try:
        if isinstance(value, Mapping):
            return sanitize_mapping(value)
        if isinstance(value, list):
            return [redact_data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(redact_data(item) for item in value)
        if isinstance(value, str):
            return sanitize_text(value)
        return value
    except Exception:  # noqa: BLE001
        return REDACTED


def redact_text(value: str) -> str:
    return sanitize_text(value)
