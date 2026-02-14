from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from btcbot.config import Settings
from btcbot.services.market_data_replay import MarketDataReplay
from btcbot.services.parity import compare_fingerprints, compute_run_fingerprint
from btcbot.services.stage7_backtest_runner import Stage7BacktestRunner


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _dataset(root: Path) -> None:
    _write_csv(
        root / "candles" / "BTCTRY.csv",
        "ts,open,high,low,close,volume",
        [
            "2024-01-01T00:00:00+00:00,100,101,99,100,10",
            "2024-01-01T00:01:00+00:00,100,102,99,101,11",
            "2024-01-01T00:02:00+00:00,101,103,100,102,12",
        ],
    )
    _write_csv(
        root / "orderbook" / "BTCTRY.csv",
        "ts,best_bid,best_ask",
        [
            "2024-01-01T00:00:00+00:00,99.9,100.1",
            "2024-01-01T00:01:00+00:00,100.9,101.1",
            "2024-01-01T00:02:00+00:00,101.9,102.1",
        ],
    )
    _write_csv(
        root / "ticker" / "BTCTRY.csv",
        "ts,last,high,low,volume,quote_volume",
        [
            "2024-01-01T00:00:00+00:00,100,101,99,10,1000",
            "2024-01-01T00:01:00+00:00,101,102,100,11,1100",
            "2024-01-01T00:02:00+00:00,102,103,101,12,1200",
        ],
    )


def _settings(db_path: Path) -> Settings:
    return Settings(
        STAGE7_ENABLED=True,
        DRY_RUN=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS='["BTCTRY"]',
        STAGE7_UNIVERSE_WHITELIST='["BTCTRY"]',
    )


def _replay(dataset: Path, seed: int) -> MarketDataReplay:
    return MarketDataReplay.from_folder(
        data_path=dataset,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        step_seconds=60,
        seed=seed,
    )


def test_stage7_backtest_no_adaptation_has_zero_param_changes(tmp_path: Path) -> None:
    dataset = tmp_path / "data"
    _dataset(dataset)
    out_db = tmp_path / "backtest.db"

    summary = Stage7BacktestRunner().run(
        settings=_settings(out_db),
        replay=_replay(dataset, seed=7),
        cycles=None,
        out_db_path=out_db,
        seed=7,
        freeze_params=True,
        disable_adaptation=True,
    )

    assert summary.param_changes == 0


def test_stage7_backtest_rerun_same_db_is_idempotent_for_cycle_rows(tmp_path: Path) -> None:
    dataset = tmp_path / "data"
    _dataset(dataset)
    out_db = tmp_path / "backtest.db"
    settings = _settings(out_db)

    Stage7BacktestRunner().run(
        settings=settings,
        replay=_replay(dataset, seed=3),
        cycles=None,
        out_db_path=out_db,
        seed=3,
        freeze_params=False,
        disable_adaptation=False,
    )
    with sqlite3.connect(out_db) as conn:
        first_count = conn.execute("SELECT COUNT(*) FROM stage7_cycle_trace").fetchone()[0]

    Stage7BacktestRunner().run(
        settings=settings,
        replay=_replay(dataset, seed=3),
        cycles=None,
        out_db_path=out_db,
        seed=3,
        freeze_params=False,
        disable_adaptation=False,
    )
    with sqlite3.connect(out_db) as conn:
        second_count = conn.execute("SELECT COUNT(*) FROM stage7_cycle_trace").fetchone()[0]

    assert second_count == first_count


def test_stage7_parity_same_seed_with_adaptation_matches(tmp_path: Path) -> None:
    dataset = tmp_path / "data"
    _dataset(dataset)

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"

    Stage7BacktestRunner().run(
        settings=_settings(db_a),
        replay=_replay(dataset, seed=11),
        cycles=None,
        out_db_path=db_a,
        seed=11,
        freeze_params=False,
        disable_adaptation=False,
    )
    Stage7BacktestRunner().run(
        settings=_settings(db_b),
        replay=_replay(dataset, seed=11),
        cycles=None,
        out_db_path=db_b,
        seed=11,
        freeze_params=False,
        disable_adaptation=False,
    )

    f1 = compute_run_fingerprint(
        db_a,
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        include_adaptation=True,
    )
    f2 = compute_run_fingerprint(
        db_b,
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        include_adaptation=True,
    )

    assert compare_fingerprints(f1, f2)
