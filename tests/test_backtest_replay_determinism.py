from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from btcbot.config import Settings
from btcbot.services.market_data_replay import MarketDataReplay
from btcbot.services.parity import compare_fingerprints, compute_run_fingerprint
from btcbot.services.stage7_backtest_runner import Stage7BacktestRunner


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _build_dataset(root: Path) -> None:
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


def _run_once(dataset: Path, db_path: Path, seed: int) -> str:
    settings = Settings(
        STAGE7_ENABLED=True,
        DRY_RUN=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS='["BTCTRY"]',
        STAGE7_UNIVERSE_WHITELIST='["BTCTRY"]',
    )
    replay = MarketDataReplay.from_folder(
        data_path=dataset,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        step_seconds=60,
        seed=seed,
    )
    Stage7BacktestRunner().run(
        settings=settings,
        replay=replay,
        cycles=None,
        out_db_path=db_path,
        seed=seed,
        freeze_params=True,
        disable_adaptation=True,
    )
    return compute_run_fingerprint(
        db_path,
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
    )


def test_replay_same_dataset_same_seed_identical_fingerprint(tmp_path: Path) -> None:
    dataset = tmp_path / "data"
    _build_dataset(dataset)

    f1 = _run_once(dataset, tmp_path / "a.db", seed=123)
    f2 = _run_once(dataset, tmp_path / "b.db", seed=123)

    assert f1 == f2


def test_replay_different_seed_still_deterministic_for_same_data(tmp_path: Path) -> None:
    dataset = tmp_path / "data"
    _build_dataset(dataset)

    f1 = _run_once(dataset, tmp_path / "a.db", seed=123)
    f2 = _run_once(dataset, tmp_path / "b.db", seed=999)

    assert f1 == f2


def test_backtest_same_seed_reports_parity_match(tmp_path: Path) -> None:
    dataset = tmp_path / "data"
    _build_dataset(dataset)

    f1 = _run_once(dataset, tmp_path / "a.db", seed=42)
    f2 = _run_once(dataset, tmp_path / "b.db", seed=42)

    assert compare_fingerprints(f1, f2)
