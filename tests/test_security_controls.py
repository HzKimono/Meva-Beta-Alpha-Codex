from __future__ import annotations

from datetime import UTC, datetime, timedelta

import logging
import pytest

from btcbot.config import Settings
from btcbot.security.redaction import redact_data, redact_text
from btcbot.security.secrets import validate_secret_controls


def test_redact_value_masks_sensitive_keys() -> None:
    payload = {
        "api_key": "abc",
        "nested": {"token": "xyz", "ok": 1},
        "headers": {"X-Signature": "sig"},
    }
    redacted = redact_data(payload)
    assert redacted["api_key"] != "abc"
    assert redacted["nested"]["token"] != "xyz"
    assert redacted["nested"]["ok"] == 1
    assert redacted["headers"]["X-Signature"] != "sig"


def test_redact_text_masks_known_patterns() -> None:
    text = "BTCTURK_API_KEY=abc BTCTURK_API_SECRET=s3cr3t"
    assert "[REDACTED]" in redact_text(text)


def test_redact_text_authorization_and_headers() -> None:
    bearer = redact_text("Authorization: Bearer abc.def.ghi")
    assert "Authorization: Bearer [REDACTED]" in bearer
    assert "abc.def.ghi" not in bearer

    authorization_equals = redact_text("authorization=abc123")
    assert authorization_equals == "authorization=[REDACTED]"

    x_api_key = redact_text("X-API-Key: secretkey")
    assert x_api_key == "X-API-Key: [REDACTED]"

    x_pck = redact_text("X-PCK=secretpck")
    assert x_pck == "X-PCK=[REDACTED]"

    x_stamp = redact_text("X-Stamp: 1700000000")
    assert x_stamp == "X-Stamp: [REDACTED]"

    legacy = redact_text("BTCTURK_API_KEY=abc BTCTURK_API_SECRET=s3cr3t X-Signature: deadbeef")
    assert "abc" not in legacy
    assert "s3cr3t" not in legacy
    assert "deadbeef" not in legacy
    assert legacy.count("[REDACTED]") == 3


def test_secret_validation_rejects_withdraw_scope() -> None:
    result = validate_secret_controls(
        scopes=["read", "trade", "withdraw"],
        rotated_at=datetime.now(UTC).isoformat(),
        max_age_days=90,
        live_trading=True,
    )
    assert result.ok is False
    assert any("withdraw" in err for err in result.errors)


def test_secret_validation_detects_expired_rotation() -> None:
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    result = validate_secret_controls(
        scopes=["read", "trade"],
        rotated_at=old,
        max_age_days=90,
        live_trading=True,
    )
    assert result.ok is False
    assert any("exceeded max age" in err for err in result.errors)


def test_live_trading_requires_safe_mode_off() -> None:
    with pytest.raises(ValueError):
        Settings(
            LIVE_TRADING=True,
            LIVE_TRADING_ACK="I_UNDERSTAND",
            DRY_RUN=False,
            KILL_SWITCH=False,
            BTCTURK_API_KEY="key",
            BTCTURK_API_SECRET="secret",
            SAFE_MODE=True,
        )


def test_safe_mode_default_is_enabled() -> None:
    settings = Settings(_env_file=None)
    assert settings.safe_mode is True


class _DummyInstrumentation:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def counter(self, name: str, value: int, attrs: dict[str, object] | None = None) -> None:
        self.calls.append(name)


def test_rotation_hygiene_warn_logs_and_metrics(monkeypatch, caplog) -> None:
    from btcbot.security import secrets

    inst = _DummyInstrumentation()
    monkeypatch.setattr(secrets, "get_instrumentation", lambda: inst)
    monkeypatch.setattr(secrets, "_utc_today", lambda: datetime(2025, 1, 15, tzinfo=UTC))

    with caplog.at_level(logging.WARNING):
        blocked = secrets.enforce_secret_rotation_hygiene(
            api_key_rotated_at="2024-12-01",
            warn_days=30,
            max_age_days=120,
        )

    assert blocked is False
    assert "secret_rotation_policy_warn_total" in inst.calls
    assert any("secret_rotation_policy_warn" in rec.message for rec in caplog.records)


def test_rotation_hygiene_expired_blocks_trading_and_metrics(monkeypatch, caplog) -> None:
    from btcbot.security import secrets

    inst = _DummyInstrumentation()
    monkeypatch.setattr(secrets, "get_instrumentation", lambda: inst)
    monkeypatch.setattr(secrets, "_utc_today", lambda: datetime(2025, 1, 15, tzinfo=UTC))

    with caplog.at_level(logging.ERROR):
        blocked = secrets.enforce_secret_rotation_hygiene(
            api_key_rotated_at="2024-01-01",
            warn_days=30,
            max_age_days=90,
        )

    assert blocked is True
    assert secrets.is_trading_blocked_by_policy() is True
    assert "secret_rotation_policy_expired_total" in inst.calls
    assert any("secret_rotation_policy_expired" in rec.message for rec in caplog.records)
