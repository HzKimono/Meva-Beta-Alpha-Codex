from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.services.market_data_replay import MarketDataReplay
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
        return BacktestSummary(**summary.__dict__)
