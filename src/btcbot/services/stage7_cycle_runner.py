from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from btcbot.config import Settings
from btcbot.domain.anomalies import combine_modes
from btcbot.domain.risk_budget import Mode
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.ledger_service import LedgerService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.state_store import StateStore

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
        try:
            mark_prices, _ = stage4._resolve_mark_prices(exchange, settings.symbols)  # noqa: SLF001
        finally:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()
        dry_cash = Decimal(str(settings.dry_run_try_balance))

        mode_payload = {
            "base_mode": Mode.NORMAL.value,
            "override_mode": None,
            "final_mode": combine_modes(Mode.NORMAL, None).value,
        }
        final_mode = Mode(mode_payload["final_mode"])

        actions: list[dict[str, object]] = []
        simulated_count = 0
        slippage_try = Decimal("0")

        if final_mode != Mode.OBSERVE_ONLY:
            open_orders = state_store.list_stage4_open_orders()
            lifecycle_actions = []
            for order in open_orders:
                if order.status.lower() == "simulated_submitted":
                    from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType

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
            simulated = ledger_service.simulate_dry_run_fills(
                actions=lifecycle_actions,
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
            ]
        else:
            actions = [{"status": "skipped", "reason": "observe_only_mode"}]

        snapshot = ledger_service.snapshot(
            mark_prices=mark_prices,
            cash_try=dry_cash,
            slippage_try=slippage_try,
            ts=now,
        )

        state_store.save_stage7_cycle(
            cycle_id=cycle_id,
            ts=now,
            selected_universe=[],
            intents_summary={
                "orders_considered": len(actions),
                "orders_simulated": simulated_count,
            },
            mode_payload=mode_payload,
            order_decisions=actions,
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
        state_store.set_last_cycle_id(cycle_id)
        return result
