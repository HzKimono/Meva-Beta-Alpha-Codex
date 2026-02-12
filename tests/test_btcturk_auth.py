from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from btcbot.adapters.btcturk_auth import build_auth_headers, compute_signature


def test_compute_signature_matches_reference_implementation() -> None:
    api_key = "demo-key"
    stamp_ms = 1700000000123
    raw_secret = b"super-secret-bytes"
    api_secret = base64.b64encode(raw_secret).decode("utf-8")

    expected = base64.b64encode(
        hmac.new(raw_secret, f"{api_key}{stamp_ms}".encode(), hashlib.sha256).digest()
    ).decode("utf-8")

    assert compute_signature(api_key, api_secret, stamp_ms) == expected


def test_compute_signature_accepts_string_stamp() -> None:
    api_key = "k"
    stamp_ms = "1700000000999"
    raw_secret = b"s"
    api_secret = base64.b64encode(raw_secret).decode("utf-8")

    expected = base64.b64encode(
        hmac.new(raw_secret, f"{api_key}{stamp_ms}".encode(), hashlib.sha256).digest()
    ).decode("utf-8")

    assert compute_signature(api_key, api_secret, stamp_ms) == expected


def test_compute_signature_rejects_invalid_base64_secret() -> None:
    with pytest.raises(ValueError, match="valid base64"):
        compute_signature("k", "%%%not_base64%%%", 1700000000000)


def test_build_auth_headers() -> None:
    api_key = "k"
    raw_secret = b"s"
    api_secret = base64.b64encode(raw_secret).decode("utf-8")
    headers = build_auth_headers(api_key=api_key, api_secret=api_secret, stamp_ms=12345)

    assert headers["X-PCK"] == api_key
    assert headers["X-Stamp"] == "12345"
    assert headers["X-Signature"] == compute_signature(api_key, api_secret, 12345)
