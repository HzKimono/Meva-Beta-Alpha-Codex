from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class MonotonicNonceGenerator:
    now_ms_fn: Callable[[], int] = field(default_factory=lambda: (lambda: int(time.time() * 1000)))
    _last_stamp_ms: int | None = None

    def next_stamp_ms(self) -> int:
        now_ms = int(self.now_ms_fn())
        if self._last_stamp_ms is not None:
            now_ms = max(now_ms, self._last_stamp_ms + 1)
        self._last_stamp_ms = now_ms
        return now_ms


def compute_signature(api_key: str, api_secret: str, stamp_ms: int | str) -> str:
    try:
        secret = base64.b64decode(api_secret, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("BTCTURK_API_SECRET must be valid base64") from exc

    message = f"{api_key}{stamp_ms}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_auth_headers(api_key: str, api_secret: str, stamp_ms: int | str) -> dict[str, str]:
    stamp = str(stamp_ms)
    return {
        "X-PCK": api_key,
        "X-Stamp": stamp,
        "X-Signature": compute_signature(api_key=api_key, api_secret=api_secret, stamp_ms=stamp),
    }
