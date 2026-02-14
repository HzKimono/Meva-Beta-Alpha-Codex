from __future__ import annotations

import logging
from dataclasses import dataclass

from btcbot.adapters.exchange_stage4 import ExchangeClientStage4
from btcbot.config import Settings
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, Quantizer
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionReport:
    executed_total: int
    submitted: int
    canceled: int
    simulated: int
    rejected: int


class ExecutionService:
    """Stage 4 execution gate. Only this service performs exchange write side effects."""

    def __init__(
        self,
        exchange: ExchangeClientStage4,
        state_store: StateStore,
        settings: Settings,
        rules_service: ExchangeRulesService,
    ) -> None:
        self.exchange = exchange
        self.state_store = state_store
        self.settings = settings
        self.rules_service = rules_service

    def execute(self, actions: list[LifecycleAction]) -> int:
        return self.execute_with_report(actions).executed_total

    def execute_with_report(self, actions: list[LifecycleAction]) -> ExecutionReport:
        if self.settings.kill_switch:
            logger.warning("kill_switch_active_blocking_writes")
            return ExecutionReport(
                executed_total=0, submitted=0, canceled=0, simulated=0, rejected=0
            )
        if self.settings.live_trading and not self.settings.is_live_trading_enabled():
            raise RuntimeError("LIVE_TRADING requires LIVE_TRADING_ACK=I_UNDERSTAND")

        live_mode = self.settings.is_live_trading_enabled() and not self.settings.dry_run
        submitted = 0
        canceled = 0
        simulated = 0
        rejected = 0
        for action in actions:
            if action.action_type == LifecycleActionType.SUBMIT:
                if not action.client_order_id:
                    logger.warning(
                        "submit_missing_client_order_id", extra={"symbol": action.symbol}
                    )
                    continue
                if self.state_store.client_order_id_exists(action.client_order_id):
                    logger.info("submit_deduped", extra={"client_order_id": action.client_order_id})
                    continue
                if self.state_store.is_order_terminal(action.client_order_id):
                    continue

                try:
                    rules = self.rules_service.get_rules(action.symbol)
                except ValueError:
                    self.state_store.record_stage4_order_rejected(
                        action.client_order_id,
                        "missing_exchange_rules",
                        symbol=action.symbol,
                        side=action.side,
                        price=action.price,
                        qty=action.qty,
                        mode=("live" if live_mode else "dry_run"),
                    )
                    rejected += 1
                    continue
                q_price = Quantizer.quantize_price(action.price, rules)
                q_qty = Quantizer.quantize_qty(action.qty, rules)
                if not Quantizer.validate_min_notional(q_price, q_qty, rules):
                    self.state_store.record_stage4_order_rejected(
                        action.client_order_id,
                        "min_notional_violation",
                        symbol=action.symbol,
                        side=action.side,
                        price=q_price,
                        qty=q_qty,
                        mode=("live" if live_mode else "dry_run"),
                    )
                    rejected += 1
                    continue

                if not live_mode:
                    self.state_store.record_stage4_order_simulated_submit(
                        symbol=action.symbol,
                        client_order_id=action.client_order_id,
                        side=action.side,
                        price=q_price,
                        qty=q_qty,
                    )
                    simulated += 1
                    continue

                ack = self.exchange.submit_limit_order(
                    symbol=action.symbol,
                    side=action.side,
                    price=q_price,
                    qty=q_qty,
                    client_order_id=action.client_order_id,
                )
                self.state_store.record_stage4_order_submitted(
                    symbol=action.symbol,
                    client_order_id=action.client_order_id,
                    exchange_order_id=ack.exchange_order_id,
                    side=action.side,
                    price=q_price,
                    qty=q_qty,
                    mode="live",
                    status="open",
                )
                submitted += 1

            if action.action_type == LifecycleActionType.CANCEL:
                client_id = action.client_order_id
                if not client_id:
                    logger.warning("cancel_missing_client_id")
                    continue
                if self.state_store.is_order_terminal(client_id):
                    continue

                order = self.state_store.get_stage4_order_by_client_id(client_id)
                exchange_id = action.exchange_order_id or (
                    order.exchange_order_id if order else None
                )
                if exchange_id is None:
                    self.state_store.record_stage4_order_error(
                        client_order_id=client_id,
                        reason="cancel_missing_exchange_order_id",
                        symbol=action.symbol,
                        side=action.side,
                        price=action.price,
                        qty=action.qty,
                        mode=("live" if live_mode else "dry_run"),
                        status="error",
                    )
                    continue

                self.state_store.record_stage4_order_cancel_requested(client_id)
                if not live_mode:
                    self.state_store.record_stage4_order_canceled(client_id)
                    canceled += 1
                    continue

                if self.exchange.cancel_order_by_exchange_id(exchange_id):
                    self.state_store.record_stage4_order_canceled(client_id)
                    canceled += 1

        return ExecutionReport(
            executed_total=submitted + canceled + simulated,
            submitted=submitted,
            canceled=canceled,
            simulated=simulated,
            rejected=rejected,
        )
