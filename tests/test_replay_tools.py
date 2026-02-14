from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from btcbot import cli
from btcbot.config import Settings
from btcbot.replay.tools import init_replay_dataset
from btcbot.replay.validate import validate_replay_dataset
from btcbot.services.parity import compare_fingerprints, compute_run_fingerprint


def test_replay_init_creates_valid_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "replay"
    init_replay_dataset(dataset_path=dataset, seed=123, write_synthetic=True)
    report = validate_replay_dataset(dataset)

    assert report.ok
    assert (dataset / "candles" / "BTCTRY.csv").exists()
    assert (dataset / "orderbook" / "BTCTRY.csv").exists()


def test_stage7_backtest_with_synthetic_dataset_determinism(tmp_path: Path) -> None:
    dataset = tmp_path / "replay"
    init_replay_dataset(dataset_path=dataset, seed=123, write_synthetic=True)

    settings = Settings(
        STAGE7_ENABLED=True,
        DRY_RUN=True,
        SYMBOLS='["BTCTRY"]',
        STAGE7_UNIVERSE_WHITELIST='["BTCTRY"]',
    )

    a_db = tmp_path / "a.db"
    b_db = tmp_path / "b.db"

    assert (
        cli.run_stage7_backtest(
            settings,
            data_path=str(dataset),
            out_db=str(a_db),
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T00:05:00Z",
            step_seconds=60,
            seed=123,
            cycles=None,
            pair_info_json=None,
            include_adaptation=True,
        )
        == 0
    )
    assert (
        cli.run_stage7_backtest(
            settings,
            data_path=str(dataset),
            out_db=str(b_db),
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T00:05:00Z",
            step_seconds=60,
            seed=123,
            cycles=None,
            pair_info_json=None,
            include_adaptation=True,
        )
        == 0
    )

    f1 = compute_run_fingerprint(
        a_db,
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        include_adaptation=True,
    )
    f2 = compute_run_fingerprint(
        b_db,
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        include_adaptation=True,
    )
    assert compare_fingerprints(f1, f2)

    with sqlite3.connect(a_db) as conn:
        first_count = conn.execute("SELECT COUNT(*) FROM stage7_cycle_trace").fetchone()[0]
        assert first_count > 0

    assert (
        cli.run_stage7_backtest(
            settings,
            data_path=str(dataset),
            out_db=str(a_db),
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T00:05:00Z",
            step_seconds=60,
            seed=123,
            cycles=None,
            pair_info_json=None,
            include_adaptation=True,
        )
        == 0
    )

    with sqlite3.connect(a_db) as conn:
        second_count = conn.execute("SELECT COUNT(*) FROM stage7_cycle_trace").fetchone()[0]
    assert second_count == first_count
