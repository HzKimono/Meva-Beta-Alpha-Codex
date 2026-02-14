from __future__ import annotations

import json

import httpx


def fetch_exchangeinfo() -> dict[str, object]:
    response = httpx.get(
        "https://api.btcturk.com/api/v2/server/exchangeinfo",
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    payload = fetch_exchangeinfo()
    pair_count = len(payload.get("data", {}).get("symbols", []))
    print(json.dumps({"symbol_count": pair_count}, sort_keys=True))
