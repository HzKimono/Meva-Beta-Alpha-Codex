from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from btcbot.security.redaction import REDACTED

logger = logging.getLogger(__name__)


class SecretProvider(Protocol):
    def get(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class EnvSecretProvider:
    def get(self, key: str) -> str | None:
        value = os.getenv(key)
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


@dataclass(frozen=True)
class DotenvSecretProvider:
    env_file: str

    def get(self, key: str) -> str | None:
        path = Path(self.env_file)
        if not path.exists():
            return None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("export "):
                    stripped = stripped[len("export ") :].strip()
                if not stripped.startswith(f"{key}="):
                    continue
                raw = stripped.split("=", 1)[1].strip()
                return raw.strip("\"'") or None
        except OSError:
            return None
        return None


@dataclass(frozen=True)
class ChainedSecretProvider:
    providers: tuple[SecretProvider, ...]

    def get(self, key: str) -> str | None:
        for provider in self.providers:
            value = provider.get(key)
            if value:
                return value
        return None


@dataclass(frozen=True)
class SecretValidationResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def build_default_provider(*, env_file: str | None = None) -> ChainedSecretProvider:
    providers: list[SecretProvider] = [EnvSecretProvider()]
    if env_file:
        providers.append(DotenvSecretProvider(env_file=env_file))
    return ChainedSecretProvider(tuple(providers))


def inject_runtime_secrets(provider: SecretProvider, *, keys: tuple[str, ...]) -> None:
    for key in keys:
        if os.getenv(key):
            continue
        value = provider.get(key)
        if value:
            os.environ[key] = value


def validate_secret_controls(
    *,
    scopes: list[str],
    rotated_at: str | None,
    max_age_days: int,
    live_trading: bool,
) -> SecretValidationResult:
    normalized_scopes = {scope.strip().lower() for scope in scopes if scope.strip()}
    errors: list[str] = []
    warnings: list[str] = []

    if "withdraw" in normalized_scopes:
        errors.append("BTCTURK_API_SCOPES must not include withdraw")

    required_scopes = {"read"}
    if live_trading:
        required_scopes.add("trade")
    missing = sorted(required_scopes - normalized_scopes)
    if missing:
        errors.append(f"Missing required API scopes: {','.join(missing)}")

    if not rotated_at:
        warnings.append("BTCTURK_SECRET_ROTATED_AT is not set; rotation age cannot be validated")
        return SecretValidationResult(tuple(errors), tuple(warnings))

    try:
        rotated_dt = datetime.fromisoformat(rotated_at.replace("Z", "+00:00"))
        if rotated_dt.tzinfo is None:
            rotated_dt = rotated_dt.replace(tzinfo=UTC)
    except ValueError:
        errors.append("BTCTURK_SECRET_ROTATED_AT must be an ISO-8601 timestamp")
        return SecretValidationResult(tuple(errors), tuple(warnings))

    age = datetime.now(UTC) - rotated_dt.astimezone(UTC)
    if age < timedelta(0):
        warnings.append("BTCTURK_SECRET_ROTATED_AT is in the future; check clock synchronization")
    elif age > timedelta(days=max_age_days):
        errors.append(
            "BTCTURK secrets exceeded max age; rotate BTCTURK_API_KEY and BTCTURK_API_SECRET"
        )

    return SecretValidationResult(tuple(errors), tuple(warnings))


def log_secret_validation(result: SecretValidationResult) -> None:
    for warning in result.warnings:
        try:
            logger.warning("secret_control_warning", extra={"extra": {"warning": warning}})
        except OSError:
            pass
    for err in result.errors:
        logger.error("secret_control_error", extra={"extra": {"error": err}})


def redact_secret_presence(key: str, value: str | None) -> dict[str, str]:
    return {"key": key, "value": REDACTED if value else "<missing>"}
