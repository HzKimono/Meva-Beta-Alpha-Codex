from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.persistence.sqlite.sqlite_connection import sqlite_connection_context
from btcbot.services.market_data_replay import MarketDataReplay
from btcbot.services.parity import compute_run_fingerprint
from btcbot.services.stage7_single_cycle_driver import (
    BacktestSummary as DriverBacktestSummary,
)
from btcbot.services.stage7_single_cycle_driver import (
    Stage7SingleCycleDriver,
)


@dataclass(frozen=True)
class BacktestSummary:
    cycles_run: int
    seed: int
    db_path: str
    started_at: str
    ended_at: str
    param_changes: int = 0
    params_checkpoints: int = 0
    final_fingerprint: str | None = None


class Stage7BacktestRunner:
    def run(
        self,
        *,
        settings: Settings,
        replay: MarketDataReplay,
        cycles: int | None,
        out_db_path: Path,
        seed: int,
        freeze_params: bool = True,
        disable_adaptation: bool = True,
        pair_info_snapshot: list[PairInfo | dict[str, object]] | None = None,
    ) -> BacktestSummary:
        summary: DriverBacktestSummary = Stage7SingleCycleDriver().run(
            settings=settings,
            replay=replay,
            cycles=cycles,
            out_db_path=out_db_path,
            seed=seed,
            freeze_params=freeze_params,
            disable_adaptation=disable_adaptation,
            pair_info_snapshot=pair_info_snapshot,
        )
        param_changes, params_checkpoints = _read_adaptation_counts(out_db_path)
        final_fingerprint = compute_run_fingerprint(
            out_db_path,
            datetime.fromisoformat(summary.started_at),
            datetime.fromisoformat(summary.ended_at),
            include_adaptation=not disable_adaptation,
        )
        return BacktestSummary(
            cycles_run=summary.cycles_run,
            seed=summary.seed,
            db_path=summary.db_path,
            started_at=summary.started_at,
            ended_at=summary.ended_at,
            param_changes=param_changes,
            params_checkpoints=params_checkpoints,
            final_fingerprint=final_fingerprint,
        )


def _read_adaptation_counts(db_path: Path) -> tuple[int, int]:
    with sqlite_connection_context(str(db_path)) as conn:
        param_changes = int(conn.execute("SELECT COUNT(*) FROM stage7_param_changes").fetchone()[0])
        checkpoints = int(
            conn.execute("SELECT COUNT(*) FROM stage7_params_checkpoints").fetchone()[0]
        )
    return param_changes, checkpoints
