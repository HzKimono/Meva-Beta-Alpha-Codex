from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from btcbot.config import Settings
from btcbot.services.market_data_replay import MarketDataReplay
from btcbot.services.parity import compute_run_fingerprint
from btcbot.services.stage7_backtest_runner import Stage7BacktestRunner
from btcbot.services.stage7_single_cycle_driver import Stage7SingleCycleDriver


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _dataset(root: Path) -> None:
    for symbol, p0 in [("BTCTRY", 100), ("ETHTRY", 50)]:
        _write_csv(
            root / "candles" / f"{symbol}.csv",
            "ts,open,high,low,close,volume",
            [
                f"2024-01-01T00:00:00+00:00,{p0},{p0 + 1},{p0 - 1},{p0},10",
                f"2024-01-01T00:01:00+00:00,{p0 + 1},{p0 + 2},{p0},{p0 + 1},10",
                f"2024-01-01T00:02:00+00:00,{p0 + 2},{p0 + 3},{p0 + 1},{p0 + 2},10",
            ],
        )
        _write_csv(
            root / "orderbook" / f"{symbol}.csv",
            "ts,best_bid,best_ask",
            [
                f"2024-01-01T00:00:00+00:00,{p0 - 0.1},{p0 + 0.1}",
                f"2024-01-01T00:01:00+00:00,{p0 + 0.9},{p0 + 1.1}",
                f"2024-01-01T00:02:00+00:00,{p0 + 1.9},{p0 + 2.1}",
            ],
        )
        _write_csv(
            root / "ticker" / f"{symbol}.csv",
            "ts,last,high,low,volume,quote_volume",
            [
                f"2024-01-01T00:00:00+00:00,{p0},{p0 + 1},{p0 - 1},10,1000",
                f"2024-01-01T00:01:00+00:00,{p0 + 1},{p0 + 2},{p0},10,1000",
                f"2024-01-01T00:02:00+00:00,{p0 + 2},{p0 + 3},{p0 + 1},10,1000",
            ],
        )


def _run_single_step_driver(settings: Settings, replay: MarketDataReplay, out_db: Path) -> None:
    runner = Stage7SingleCycleDriver()
    runner.run(
        settings=settings,
        replay=replay,
        cycles=None,
        out_db_path=out_db,
        seed=123,
        freeze_params=True,
        disable_adaptation=True,
    )


def test_backtest_runner_parity_with_equivalent_pipeline(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _dataset(data)
    settings = Settings(
        STAGE7_ENABLED=True,
        DRY_RUN=True,
        SYMBOLS='["BTCTRY","ETHTRY"]',
        STAGE7_UNIVERSE_WHITELIST='["BTCTRY","ETHTRY"]',
    )

    replay_a = MarketDataReplay.from_folder(
        data_path=data,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        step_seconds=60,
        seed=123,
    )
    replay_b = MarketDataReplay.from_folder(
        data_path=data,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        step_seconds=60,
        seed=123,
    )

    db_runner = tmp_path / "runner.db"
    db_equiv = tmp_path / "equiv.db"

    Stage7BacktestRunner().run(
        settings=settings,
        replay=replay_a,
        cycles=None,
        out_db_path=db_runner,
        seed=123,
        freeze_params=True,
        disable_adaptation=True,
    )
    _run_single_step_driver(settings, replay_b, db_equiv)

    f_runner = compute_run_fingerprint(
        db_runner,
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
    )
    f_equiv = compute_run_fingerprint(
        db_equiv,
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
    )

    assert f_runner == f_equiv


def test_parity_fingerprint_handles_null_json_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "nulls.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE stage7_cycle_trace (
                cycle_id TEXT,
                ts TEXT,
                selected_universe_json TEXT,
                intents_summary_json TEXT,
                mode_json TEXT,
                active_param_version INTEGER,
                param_change_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE stage7_ledger_metrics (
                cycle_id TEXT,
                net_pnl_try TEXT,
                fees_try TEXT,
                slippage_try TEXT,
                turnover_try TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO stage7_cycle_trace VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c1", "2024-01-01T00:00:00+00:00", None, None, None, None, None),
        )
        conn.execute(
            "INSERT INTO stage7_ledger_metrics VALUES (?, ?, ?, ?, ?)",
            ("c1", "1.23", "0.10", "0.00", "5.00"),
        )

    fingerprint = compute_run_fingerprint(
        db_path,
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
    )

    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64


def test_backtest_runner_calls_stage7_cycle_runner_run_one_cycle(
    monkeypatch, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    _dataset(data)
    replay = MarketDataReplay.from_folder(
        data_path=data,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        step_seconds=60,
        seed=123,
    )
    settings = Settings(STAGE7_ENABLED=True, DRY_RUN=True, SYMBOLS='["BTCTRY","ETHTRY"]')

    calls: list[str] = []

    def _spy_run_one_cycle(self, settings, **kwargs):
        del self, settings, kwargs
        calls.append("called")
        return 0

    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.Stage7CycleRunner.run_one_cycle", _spy_run_one_cycle
    )

    Stage7BacktestRunner().run(
        settings=settings,
        replay=replay,
        cycles=1,
        out_db_path=tmp_path / "runner.db",
        seed=123,
    )

    assert calls == ["called"]
