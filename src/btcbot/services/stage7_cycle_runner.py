from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from btcbot.config import Settings
from btcbot.domain.anomalies import combine_modes
from btcbot.domain.models import Balance, normalize_symbol
from btcbot.domain.risk_budget import Mode
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.ledger_service import LedgerService
from btcbot.services.order_builder_service import OrderBuilderService
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.state_store import StateStore
from btcbot.services.universe_selection_service import UniverseSelectionService

logger = logging.getLogger(__name__)


class Stage7CycleRunner:
    command: str = "stage7-run"

    def run_one_cycle(self, settings: Settings) -> int:
        if not settings.dry_run:
            raise RuntimeError("stage7-run only supports --dry-run")

        cycle_id = uuid4().hex
        now = datetime.now(UTC)
        state_store = StateStore(db_path=settings.state_db_path)
        stage4 = Stage4CycleRunner(command=self.command)
        result = stage4.run_one_cycle(settings)

        ledger_service = LedgerService(state_store=state_store, logger=logger)
        exchange = build_exchange_stage4(settings, dry_run=True)
        universe_service = UniverseSelectionService()
        policy_service = PortfolioPolicyService()
        order_builder = OrderBuilderService()

        open_orders = state_store.list_stage4_open_orders()
        lifecycle_actions: list[LifecycleAction] = []
        for order in open_orders:
            if order.status.lower() == "simulated_submitted":
                lifecycle_actions.append(
                    LifecycleAction(
                        action_type=LifecycleActionType.SUBMIT,
                        symbol=order.symbol,
                        side=order.side,
                        price=order.price,
                        qty=order.qty,
                        reason="stage7_dry_run_simulation",
                        client_order_id=order.client_order_id,
                        exchange_order_id=order.exchange_order_id,
                    )
                )

        base_client = getattr(exchange, "client", exchange)
        rules_service = ExchangeRulesService(
            base_client,
            cache_ttl_sec=settings.rules_cache_ttl_sec,
            settings=settings,
        )

        try:
            universe_result = universe_service.select_universe(
                exchange=base_client,
                settings=settings,
                now_utc=now,
            )
            symbols_needed = sorted(
                {normalize_symbol(symbol) for symbol in universe_result.selected_symbols}
                | {normalize_symbol(action.symbol) for action in lifecycle_actions}
            )
            mark_prices, _ = stage4.resolve_mark_prices(exchange, symbols_needed)
            get_balances = getattr(base_client, "get_balances", None)
            balances = get_balances() if callable(get_balances) else []
            if not balances:
                balances = [
                    Balance(
                        asset=str(settings.stage7_universe_quote_ccy).upper(),
                        free=settings.dry_run_try_balance,
                    )
                ]

            rules_symbols_fallback: list[str] = []
            rules_symbols_invalid: list[str] = []
            rules_symbols_missing: list[str] = []
            rules_unavailable: dict[str, str] = {}
            for symbol in sorted({normalize_symbol(s) for s in universe_result.selected_symbols}):
                _, status = rules_service.get_symbol_rules_status(symbol)
                if status == "fallback":
                    rules_symbols_fallback.append(symbol)
                elif status == "invalid":
                    rules_symbols_invalid.append(symbol)
                    rules_unavailable[symbol] = status
                elif status == "missing":
                    rules_symbols_missing.append(symbol)
                    rules_unavailable[symbol] = status

            rules_stats = {
                "rules_fallback_used_count": len(rules_symbols_fallback),
                "rules_invalid_count": len(rules_symbols_invalid),
                "rules_missing_count": len(rules_symbols_missing),
                "rules_symbols_fallback": rules_symbols_fallback,
                "rules_symbols_invalid": rules_symbols_invalid,
                "rules_symbols_missing": rules_symbols_missing,
            }

            base_mode = state_store.get_latest_risk_mode()
            final_mode = combine_modes(base_mode, None)
            invalid_policy = settings.stage7_rules_invalid_metadata_policy
            if invalid_policy == "observe_only_cycle" and (
                rules_stats["rules_invalid_count"] > 0 or rules_stats["rules_missing_count"] > 0
            ):
                final_mode = Mode.OBSERVE_ONLY

            mode_payload = {
                "base_mode": base_mode.value,
                "override_mode": None,
                "final_mode": final_mode.value,
            }

            portfolio_plan = policy_service.build_plan(
                universe=universe_result.selected_symbols,
                mark_prices_try=mark_prices,
                balances=balances,
                settings=settings,
                now_utc=now,
                final_mode=final_mode,
            )

            order_intents = order_builder.build_intents(
                cycle_id=cycle_id,
                plan=portfolio_plan,
                mark_prices_try=mark_prices,
                rules=rules_service,
                settings=settings,
                final_mode=final_mode,
                now_utc=now,
                rules_unavailable=rules_unavailable,
            )

            actions: list[dict[str, object]] = []
            simulated_count = 0
            slippage_try = Decimal("0")

            if final_mode != Mode.OBSERVE_ONLY:
                filtered_actions: list[LifecycleAction] = []
                skipped_actions: list[dict[str, object]] = []
                for action in lifecycle_actions:
                    normalized_symbol = normalize_symbol(action.symbol)
                    if final_mode == Mode.REDUCE_RISK_ONLY and action.side.upper() != "SELL":
                        skipped_actions.append(
                            {
                                "symbol": normalized_symbol,
                                "side": action.side,
                                "qty": str(action.qty),
                                "status": "skipped",
                                "reason": "mode_reduce_risk_only",
                            }
                        )
                        continue
                    if normalized_symbol not in mark_prices:
                        skipped_actions.append(
                            {
                                "symbol": normalized_symbol,
                                "side": action.side,
                                "qty": str(action.qty),
                                "status": "skipped",
                                "reason": "missing_mark_price",
                            }
                        )
                        continue
                    filtered_actions.append(action)

                simulated = ledger_service.simulate_dry_run_fills(
                    cycle_id=cycle_id,
                    actions=filtered_actions,
                    mark_prices=mark_prices,
                    slippage_bps=settings.stage7_slippage_bps,
                    fees_bps=settings.stage7_fees_bps,
                    ts=now,
                )
                ingest = ledger_service.append_simulated_fills(simulated)
                simulated_count = ingest.events_inserted
                for fill in simulated:
                    slippage_try += (
                        fill.applied_price - fill.baseline_price
                    ).copy_abs() * fill.event.qty
                actions = [
                    {
                        "symbol": fill.event.symbol,
                        "side": fill.event.side,
                        "qty": str(fill.event.qty),
                        "status": "submitted",
                        "reason": "dry_run_fill_simulated",
                    }
                    for fill in simulated
                ] + skipped_actions
            else:
                actions = [{"status": "skipped", "reason": "observe_only"}]

            snapshot = ledger_service.snapshot(
                mark_prices=mark_prices,
                cash_try=Decimal(str(settings.dry_run_try_balance)),
                slippage_try=slippage_try,
                ts=now,
            )

            planned_count = sum(1 for intent in order_intents if not intent.skipped)
            skipped_count = sum(1 for intent in order_intents if intent.skipped)
            state_store.save_stage7_cycle(
                cycle_id=cycle_id,
                ts=now,
                selected_universe=universe_result.selected_symbols,
                universe_scores=[
                    {
                        "symbol": item.symbol,
                        "total_score": str(item.total_score),
                        "breakdown": item.breakdown,
                    }
                    for item in universe_result.scored[: max(0, settings.stage7_universe_size)]
                ],
                intents_summary={
                    "lifecycle_actions_total": len(actions),
                    "orders_simulated": simulated_count,
                    "order_intents_total": len(order_intents),
                    "order_intents_planned": planned_count,
                    "order_intents_skipped": skipped_count,
                    "rules_stats": rules_stats,
                },
                mode_payload=mode_payload,
                order_decisions=actions,
                portfolio_plan=portfolio_plan.to_dict(),
                order_intents=order_intents,
                order_intents_trace=[
                    {
                        "client_order_id": intent.client_order_id,
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "skipped": intent.skipped,
                        "skip_reason": intent.skip_reason,
                    }
                    for intent in order_intents
                ],
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
            )
        finally:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()

        state_store.set_last_stage7_cycle_id(cycle_id)
        return result
