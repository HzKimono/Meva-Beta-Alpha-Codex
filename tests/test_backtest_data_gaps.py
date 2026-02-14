from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from btcbot.services.market_data_replay import MarketDataReplay


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_data_gaps_use_deterministic_carry_forward(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(
        data / "candles" / "BTCTRY.csv",
        "ts,open,high,low,close,volume",
        [
            "2024-01-01T00:00:00+00:00,100,101,99,100,10",
            "2024-01-01T00:02:00+00:00,102,103,101,102,12",
        ],
    )
    _write_csv(
        data / "orderbook" / "BTCTRY.csv",
        "ts,best_bid,best_ask",
        [
            "2024-01-01T00:00:00+00:00,99.9,100.1",
            "2024-01-01T00:02:00+00:00,101.9,102.1",
        ],
    )
    _write_csv(
        data / "ticker" / "BTCTRY.csv",
        "ts,last,high,low,volume,quote_volume",
        ["2024-01-01T00:00:00+00:00,100,101,99,10,1000"],
    )

    replay = MarketDataReplay.from_folder(
        data_path=data,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        step_seconds=60,
        seed=7,
    )

    bid0, ask0 = replay.get_orderbook("BTCTRY")
    assert (bid0, ask0) == (Decimal("99.9"), Decimal("100.1"))
    assert replay.advance() is True
    bid1, ask1 = replay.get_orderbook("BTCTRY")
    assert (bid1, ask1) == (Decimal("99.9"), Decimal("100.1"))
    candles = replay.get_candles("BTCTRY", limit=5)
    assert len(candles) == 1
    stats = replay.get_ticker_stats()
    assert stats and stats[0]["pairSymbol"] == "BTCTRY"


def test_orderbook_gap_without_prior_snapshot_returns_none(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(
        data / "candles" / "BTCTRY.csv",
        "ts,open,high,low,close,volume",
        ["2024-01-01T00:00:00+00:00,100,101,99,100,10"],
    )
    _write_csv(
        data / "orderbook" / "BTCTRY.csv",
        "ts,best_bid,best_ask",
        ["2024-01-01T00:01:00+00:00,100,101"],
    )

    replay = MarketDataReplay.from_folder(
        data_path=data,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
        step_seconds=60,
        seed=9,
    )

    assert replay.get_orderbook("BTCTRY") is None


def test_replay_parses_millisecond_epoch_timestamps(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(
        data / "candles" / "BTCTRY.csv",
        "ts,open,high,low,close,volume",
        ["1704067200000,100,101,99,100,10"],
    )
    _write_csv(
        data / "orderbook" / "BTCTRY.csv",
        "ts,best_bid,best_ask",
        ["1704067200000,99.9,100.1"],
    )

    replay = MarketDataReplay.from_folder(
        data_path=data,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        step_seconds=60,
        seed=5,
    )

    candles = replay.get_candles("BTCTRY", limit=1)
    assert candles and candles[0].ts == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def test_replay_parses_second_epoch_timestamps(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(
        data / "candles" / "BTCTRY.csv",
        "ts,open,high,low,close,volume",
        ["1704067200,100,101,99,100,10"],
    )
    _write_csv(
        data / "orderbook" / "BTCTRY.csv",
        "ts,best_bid,best_ask",
        ["1704067200,99.9,100.1"],
    )

    replay = MarketDataReplay.from_folder(
        data_path=data,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        step_seconds=60,
        seed=3,
    )

    candles = replay.get_candles("BTCTRY", limit=1)
    assert candles and candles[0].ts == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
