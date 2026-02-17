from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btcbot.config import Settings
from btcbot.security.redaction import REDACTED, redact_text, redact_value
from btcbot.security.secrets import validate_secret_controls


def test_redact_value_masks_sensitive_keys() -> None:
    payload = {
        "api_key": "abc",
        "nested": {"token": "xyz", "ok": 1},
        "headers": {"X-Signature": "sig"},
    }
    redacted = redact_value(payload)
    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["token"] == REDACTED
    assert redacted["nested"]["ok"] == 1
    assert redacted["headers"]["X-Signature"] == REDACTED


def test_redact_text_masks_known_patterns() -> None:
    text = "BTCTURK_API_KEY=abc BTCTURK_API_SECRET=s3cr3t"
    assert REDACTED in redact_text(text)


def test_redact_text_authorization_and_headers() -> None:
    bearer = redact_text("Authorization: Bearer abc.def.ghi")
    assert f"Authorization: Bearer {REDACTED}" in bearer
    assert "abc.def.ghi" not in bearer

    authorization_equals = redact_text("authorization=abc123")
    assert authorization_equals == f"authorization={REDACTED}"

    x_api_key = redact_text("X-API-Key: secretkey")
    assert x_api_key == f"X-API-Key: {REDACTED}"

    x_pck = redact_text("X-PCK=secretpck")
    assert x_pck == f"X-PCK={REDACTED}"

    x_stamp = redact_text("X-Stamp: 1700000000")
    assert x_stamp == f"X-Stamp: {REDACTED}"

    legacy = redact_text("BTCTURK_API_KEY=abc BTCTURK_API_SECRET=s3cr3t X-Signature: deadbeef")
    assert "abc" not in legacy
    assert "s3cr3t" not in legacy
    assert "deadbeef" not in legacy
    assert legacy.count(REDACTED) == 3


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
