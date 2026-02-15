from __future__ import annotations

import json
from pathlib import Path

import httpx


def main() -> int:
    out = Path("tests/fixtures/btcturk_exchangeinfo_live_capture.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    response = httpx.get("https://api.btcturk.com/api/v2/server/exchangeinfo", timeout=20.0)
    response.raise_for_status()
    payload = response.json()

    sanitized = {
        "success": bool(payload.get("success", True)),
        "data": payload.get("data", {}),
    }
    out.write_text(json.dumps(sanitized, indent=2, sort_keys=True), encoding="utf-8")
    print(f"captured fixture: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
