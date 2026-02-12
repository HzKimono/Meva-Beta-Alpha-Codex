from __future__ import annotations

import base64
import hashlib
import hmac


def compute_signature(api_key: str, api_secret: str, stamp_ms: int | str) -> str:
    """Compute BTCTurk V1 HMAC-SHA256 signature.

    Signature message is ``api_key + stamp_ms`` where stamp_ms is nonce milliseconds.
    The API secret is expected as base64 text.
    """
    try:
        secret = base64.b64decode(api_secret, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("BTCTURK_API_SECRET must be valid base64") from exc

    message = f"{api_key}{stamp_ms}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_auth_headers(api_key: str, api_secret: str, stamp_ms: int | str) -> dict[str, str]:
    """Build BTCTurk V1 auth headers for private endpoints (placeholder for Stage 1)."""
    stamp = str(stamp_ms)
    return {
        "X-PCK": api_key,
        "X-Stamp": stamp,
        "X-Signature": compute_signature(api_key=api_key, api_secret=api_secret, stamp_ms=stamp),
    }
