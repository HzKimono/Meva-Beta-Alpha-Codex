from __future__ import annotations

from decimal import Decimal

from btcbot.adapters.btcturk.market_data import MarketDataSnapshotBuilder, should_observe_only


def test_market_data_staleness_detection() -> None:
    builder = MarketDataSnapshotBuilder()
    builder.ingest_orderbook(symbol="BTCTRY", bid=Decimal("1"), ask=Decimal("2"), ts_ms=1_000)
    builder.ingest_trade(symbol="BTCTRY", price=Decimal("1.5"), qty=Decimal("0.1"), ts_ms=1_000)

    fresh = builder.build(["BTCTRY"], max_age_ms=200, now_ms=1_100)
    assert fresh.is_fresh is True
    assert should_observe_only(fresh) is False

    stale = builder.build(["BTCTRY"], max_age_ms=50, now_ms=1_100)
    assert stale.is_fresh is False
    assert stale.snapshots["BTCTRY"].is_fresh is False
    assert "BTCTRY:stale_top" in stale.stale_reasons
    assert should_observe_only(stale) is True
