from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from pathlib import Path

from btcbot.adapters.replay_exchange import ReplayExchangeClient
from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.services.market_data_replay import MarketDataReplay
from btcbot.services.stage7_cycle_runner import Stage7CycleRunner
from btcbot.services.state_store import StateStore


@dataclass(frozen=True)
class BacktestSummary:
    cycles_run: int
    seed: int
    db_path: str
    started_at: str
    ended_at: str
    final_fingerprint: str | None = None


class Stage7SingleCycleDriver:
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
        effective_settings = settings.model_copy(
            update={
                "dry_run": True,
                "state_db_path": str(out_db_path),
            }
        )
        started_at = replay.now().astimezone(UTC)

        quote_asset = str(effective_settings.stage7_universe_quote_ccy).upper()
        exchange = ReplayExchangeClient(
            replay=replay,
            symbols=effective_settings.symbols,
            balances={quote_asset: Decimal(str(effective_settings.dry_run_try_balance))},
            pair_info_snapshot=pair_info_snapshot,
        )
        state_store = StateStore(db_path=str(out_db_path))
        runner = Stage7CycleRunner()

        cycle_count = 0
        while True:
            now_utc = replay.now().astimezone(UTC)
            cycle_id = f"bt:{now_utc.strftime('%Y%m%d%H%M%S')}:{cycle_count:06d}"
            runner.run_one_cycle(
                effective_settings,
                exchange=exchange,
                state_store=state_store,
                now_utc=now_utc,
                cycle_id=cycle_id,
                run_id=f"bt-seed-{seed}",
                stage4_result=0,
                enable_adaptation=not disable_adaptation,
                use_active_params=True,
            )

            cycle_count += 1
            if cycles is not None and cycle_count >= cycles:
                break
            if not replay.advance():
                break

        ended_at = replay.now().astimezone(UTC)
        return BacktestSummary(
            cycles_run=cycle_count,
            seed=seed,
            db_path=str(out_db_path),
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            final_fingerprint=None,
        )
