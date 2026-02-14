from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from pathlib import Path

from btcbot.config import Settings
from btcbot.domain.anomalies import combine_modes
from btcbot.domain.models import normalize_symbol
from btcbot.services.adaptation_service import AdaptationService
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.ledger_service import LedgerService
from btcbot.services.market_data_replay import MarketDataReplay
from btcbot.services.oms_service import OMSService, Stage7MarketSimulator
from btcbot.services.order_builder_service import OrderBuilderService
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.state_store import StateStore
from btcbot.services.universe_selection_service import UniverseSelectionService


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
    ) -> BacktestSummary:
        effective_settings = settings.model_copy(
            update={
                "dry_run": True,
                "state_db_path": str(out_db_path),
            }
        )
        state_store = StateStore(db_path=str(out_db_path))
        started_at = replay.now().astimezone(UTC)

        from btcbot.adapters.replay_exchange import ReplayExchangeClient

        exchange = ReplayExchangeClient(
            replay=replay,
            symbols=effective_settings.symbols,
            quote_asset=str(effective_settings.stage7_universe_quote_ccy).upper(),
            balance_try=Decimal(str(effective_settings.dry_run_try_balance)),
        )

        stage4 = Stage4CycleRunner(command="stage7-backtest")
        universe_service = UniverseSelectionService()
        policy_service = PortfolioPolicyService()
        order_builder = OrderBuilderService()
        ledger_service = LedgerService(state_store=state_store, logger=logging.getLogger(__name__))
        rules_service = ExchangeRulesService(
            exchange,
            cache_ttl_sec=effective_settings.rules_cache_ttl_sec,
            settings=effective_settings,
        )
        adaptation_service = AdaptationService()

        cycle_count = 0
        while True:
            now = replay.now().astimezone(UTC)
            cycle_id = f"bt:{now.strftime('%Y%m%d%H%M%S')}:{cycle_count:06d}"

            runtime = effective_settings.model_copy(deep=True)
            if freeze_params:
                active = state_store.get_active_stage7_params(settings=runtime, now_utc=now)
                runtime.stage7_universe_size = active.universe_size
                runtime.stage7_score_weights = {
                    k: float(v) for k, v in active.score_weights.items()
                }
                runtime.stage7_max_spread_bps = Decimal(str(active.max_spread_bps))
                runtime.notional_cap_try_per_cycle = active.turnover_cap_try
                runtime.max_orders_per_cycle = active.max_orders_per_cycle
                runtime.try_cash_target = active.cash_target_try
                runtime.stage7_order_offset_bps = Decimal(str(active.order_offset_bps))
                runtime.stage7_min_quote_volume_try = active.min_quote_volume_try

            universe_result = universe_service.select_universe(
                exchange=exchange,
                settings=runtime,
                now_utc=now,
            )
            mark_prices, _ = stage4.resolve_mark_prices(exchange, universe_result.selected_symbols)
            base_mode = state_store.get_latest_risk_mode()
            final_mode = combine_modes(base_mode, None)

            plan = policy_service.build_plan(
                universe=universe_result.selected_symbols,
                mark_prices_try=mark_prices,
                balances=exchange.get_balances(),
                settings=runtime,
                now_utc=now,
                final_mode=final_mode,
            )
            intents = order_builder.build_intents(
                cycle_id=cycle_id,
                plan=plan,
                mark_prices_try=mark_prices,
                rules=rules_service,
                settings=runtime,
                final_mode=final_mode,
                now_utc=now,
                rules_unavailable={},
            )

            oms_service = OMSService()
            market_sim = Stage7MarketSimulator(mark_prices)
            _, reconcile_events = oms_service.reconcile_open_orders(
                cycle_id=cycle_id,
                now_utc=now,
                state_store=state_store,
                settings=runtime,
                market_sim=market_sim,
            )
            orders, order_events = oms_service.process_intents(
                cycle_id=cycle_id,
                now_utc=now,
                intents=[item for item in intents if not item.skipped],
                market_sim=market_sim,
                state_store=state_store,
                settings=runtime,
                cancel_requests=[],
            )
            all_events = [*reconcile_events, *order_events]

            snapshot = ledger_service.snapshot(
                mark_prices=mark_prices,
                cash_try=Decimal(str(runtime.dry_run_try_balance)),
                slippage_try=Decimal("0"),
                ts=now,
            )
            state_store.save_stage7_cycle(
                cycle_id=cycle_id,
                ts=now,
                selected_universe=sorted(universe_result.selected_symbols),
                universe_scores=[
                    {
                        "symbol": item.symbol,
                        "total_score": str(item.total_score),
                        "breakdown": item.breakdown,
                    }
                    for item in universe_result.scored[: max(0, runtime.stage7_universe_size)]
                ],
                intents_summary={
                    "order_intents_total": len(intents),
                    "order_intents_planned": sum(1 for x in intents if not x.skipped),
                    "order_intents_skipped": sum(1 for x in intents if x.skipped),
                    "events_total": len(all_events),
                    "oms_summary": {
                        "orders_total": len(orders),
                        "orders_filled": sum(1 for o in orders if o.status.value == "FILLED"),
                        "orders_rejected": sum(1 for o in orders if o.status.value == "REJECTED"),
                    },
                },
                mode_payload={
                    "base_mode": base_mode.value,
                    "override_mode": None,
                    "final_mode": final_mode.value,
                },
                order_decisions=[
                    {
                        "symbol": normalize_symbol(item.symbol),
                        "side": item.side,
                        "qty": str(item.qty),
                        "status": "planned" if not item.skipped else "skipped",
                        "reason": item.skip_reason or item.reason,
                    }
                    for item in intents
                ],
                portfolio_plan=plan.to_dict(),
                order_intents=intents,
                ledger_metrics={
                    "gross_pnl_try": snapshot.gross_pnl_try,
                    "realized_pnl_try": snapshot.realized_pnl_try,
                    "unrealized_pnl_try": snapshot.unrealized_pnl_try,
                    "net_pnl_try": snapshot.net_pnl_try,
                    "fees_try": snapshot.fees_try,
                    "slippage_try": snapshot.slippage_try,
                    "turnover_try": snapshot.turnover_try,
                    "equity_try": snapshot.equity_try,
                    "max_drawdown": snapshot.max_drawdown,
                },
                active_param_version=state_store.get_active_stage7_params(
                    settings=runtime, now_utc=now
                ).version,
            )
            state_store.save_stage7_run_metrics(
                cycle_id,
                {
                    "ts": now.isoformat(),
                    "run_id": f"bt-seed-{seed}",
                    "mode_base": base_mode.value,
                    "mode_override": "",
                    "mode_final": final_mode.value,
                    "universe_size": len(universe_result.selected_symbols),
                    "intents_planned_count": sum(1 for x in intents if not x.skipped),
                    "intents_skipped_count": sum(1 for x in intents if x.skipped),
                    "oms_submitted_count": len(orders),
                    "events_appended": len(all_events),
                    "events_ignored": 0,
                    "oms_filled_count": sum(1 for o in orders if o.status.value == "FILLED"),
                    "oms_rejected_count": sum(1 for o in orders if o.status.value == "REJECTED"),
                    "oms_canceled_count": sum(1 for o in orders if o.status.value == "CANCELED"),
                    "equity_try": snapshot.equity_try,
                    "gross_pnl_try": snapshot.gross_pnl_try,
                    "net_pnl_try": snapshot.net_pnl_try,
                    "fees_try": snapshot.fees_try,
                    "slippage_try": snapshot.slippage_try,
                    "max_drawdown_pct": snapshot.max_drawdown,
                    "turnover_try": snapshot.turnover_try,
                    "latency_ms_total": 0,
                    "selection_ms": 0,
                    "planning_ms": 0,
                    "intents_ms": 0,
                    "oms_ms": 0,
                    "ledger_ms": 0,
                    "persist_ms": 0,
                    "quality_flags": {},
                    "alert_flags": {},
                },
            )
            state_store.set_last_stage7_cycle_id(cycle_id)
            if not disable_adaptation:
                adaptation_service.evaluate_and_apply(
                    state_store=state_store,
                    settings=runtime,
                    now_utc=now,
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
