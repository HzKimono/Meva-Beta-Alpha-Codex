from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from btcbot.adapters.btcturk.market_data import MarketDataSnapshotBuilder


def _load_trade_fixture() -> dict[str, object]:
    fixture = Path("tests/fixtures/btcturk_ws/channel_423_trade_match.json")
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid fixture payload")
    return payload


def test_ws_ingest_load_and_snapshot_build_performance() -> None:
    builder = MarketDataSnapshotBuilder()
    symbols = ["BTCTRY", "ETHTRY", "SOLTRY"]
    now = datetime.now(UTC)

    start = perf_counter()
    for idx in range(30_000):
        ts_ms = int((now + timedelta(milliseconds=idx)).timestamp() * 1000)
        price = Decimal("100000") + Decimal(idx % 100)
        builder.ingest_orderbook(
            symbol=symbols[idx % 3], bid=price, ask=price + Decimal("1"), ts_ms=ts_ms
        )
        builder.ingest_trade(symbol=symbols[idx % 3], price=price, qty=Decimal("0.01"), ts_ms=ts_ms)
        if idx % 50 == 0:
            result = builder.build(symbols, max_age_ms=2_000, now_ms=ts_ms)
            assert len(result.snapshots) == len(symbols)
    elapsed = perf_counter() - start

    assert elapsed < 3.0


def test_24h_simulated_soak_with_recorded_fixture() -> None:
    payload = _load_trade_fixture()
    builder = MarketDataSnapshotBuilder()
    symbols = ["BTCTRY"]
    base_price = Decimal("100000")
    cycles = 24 * 60

    for minute in range(cycles):
        ts_ms = 1_700_000_000_000 + minute * 60_000
        drift = Decimal(minute % 120) / Decimal("10")
        builder.ingest_trade(
            symbol="BTCTRY",
            price=base_price + drift,
            qty=Decimal(str(payload.get("Quantity", "0.01"))),
            ts_ms=ts_ms,
        )
        builder.ingest_orderbook(
            symbol="BTCTRY",
            bid=base_price + drift,
            ask=base_price + drift + Decimal("2"),
            ts_ms=ts_ms,
        )
        result = builder.build(symbols, max_age_ms=65_000, now_ms=ts_ms)
        assert result.is_fresh is True

    final = builder.build(symbols, max_age_ms=65_000, now_ms=1_700_000_000_000 + cycles * 60_000)
    assert final.snapshots["BTCTRY"].top is not None
