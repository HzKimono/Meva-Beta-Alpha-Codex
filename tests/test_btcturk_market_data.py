from __future__ import annotations

from decimal import Decimal

from btcbot.adapters.btcturk.market_data import MarketDataSnapshotBuilder, should_observe_only


def test_live_top_present_trade_missing_does_not_observe_only() -> None:
    builder = MarketDataSnapshotBuilder()
    builder.ingest_orderbook(symbol="BTCTRY", bid=Decimal("1"), ask=Decimal("2"), ts_ms=1_000)

    result = builder.build(["BTCTRY"], max_age_ms=200, now_ms=1_100, mode="LIVE")

    assert result.is_fresh is True
    assert result.decision.decision == "DEGRADE"
    assert should_observe_only(result) is False
    assert result.decision.missing_trade_symbols == frozenset({"BTCTRY"})


def test_missing_top_for_many_symbols_triggers_observe_only_by_ratio() -> None:
    builder = MarketDataSnapshotBuilder()
    builder.ingest_orderbook(symbol="BTCTRY", bid=Decimal("1"), ask=Decimal("2"), ts_ms=1_000)

    result = builder.build(
        ["BTCTRY", "ETHTRY", "XRPTRY", "SOLTRY"],
        max_age_ms=200,
        now_ms=1_100,
        stale_ratio_threshold=0.5,
    )

    assert result.decision.decision == "OBSERVE_ONLY"
    assert should_observe_only(result) is True
    assert result.decision.stale_symbols == frozenset({"ETHTRY", "XRPTRY", "SOLTRY"})


def test_single_stale_symbol_does_not_global_stop_when_ratio_below_threshold() -> None:
    builder = MarketDataSnapshotBuilder()
    builder.ingest_orderbook(symbol="BTCTRY", bid=Decimal("1"), ask=Decimal("2"), ts_ms=1_000)
    builder.ingest_orderbook(symbol="ETHTRY", bid=Decimal("3"), ask=Decimal("4"), ts_ms=1_000)

    result = builder.build(
        ["BTCTRY", "ETHTRY", "XRPTRY"],
        max_age_ms=200,
        now_ms=1_100,
        stale_ratio_threshold=0.5,
    )

    assert result.decision.decision == "DEGRADE"
    assert result.is_fresh is True
    assert should_observe_only(result) is False
    assert result.decision.stale_symbols == frozenset({"XRPTRY"})
    assert result.tradable_symbols == ("BTCTRY", "ETHTRY")


def test_true_stale_age_exceeded_still_observe_only() -> None:
    builder = MarketDataSnapshotBuilder()
    builder.ingest_orderbook(symbol="BTCTRY", bid=Decimal("1"), ask=Decimal("2"), ts_ms=1_000)
    builder.ingest_orderbook(symbol="ETHTRY", bid=Decimal("3"), ask=Decimal("4"), ts_ms=1_000)

    result = builder.build(["BTCTRY", "ETHTRY"], max_age_ms=10, now_ms=1_100)

    assert result.is_fresh is False
    assert result.decision.decision == "OBSERVE_ONLY"
    assert result.snapshots["BTCTRY"].is_fresh is False
    assert "BTCTRY:stale_top" in result.stale_reasons
    assert should_observe_only(result) is True
