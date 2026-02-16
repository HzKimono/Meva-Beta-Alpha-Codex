from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.accounting.accounting_service import AccountingService
from btcbot.services.execution_service import ExecutionService
from btcbot.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartupRecoveryResult:
    observe_only_required: bool
    invariant_errors: tuple[str, ...]
    fills_inserted: int


class StartupRecoveryService:
    def run(
        self,
        *,
        cycle_id: str,
        symbols: list[str],
        execution_service: ExecutionService,
        accounting_service: AccountingService,
        portfolio_service: PortfolioService,
    ) -> StartupRecoveryResult:
        logger.info("startup_recovery_started", extra={"extra": {"cycle_id": cycle_id}})

        refresh_order_lifecycle = getattr(execution_service, "refresh_order_lifecycle", None)
        if callable(refresh_order_lifecycle):
            refresh_order_lifecycle(symbols)
        fills_inserted = accounting_service.refresh(symbols, mark_prices={})

        invariant_errors: list[str] = []
        balances = portfolio_service.get_balances()
        for balance in balances:
            free = Decimal(str(balance.free))
            if free < 0:
                invariant_errors.append(f"negative_balance:{balance.asset}")

        for position in accounting_service.get_positions():
            qty = getattr(position, "qty", None)
            symbol = getattr(position, "symbol", "unknown")
            if qty is None:
                continue
            if Decimal(str(qty)) < 0:
                invariant_errors.append(f"negative_position_qty:{symbol}")

        if invariant_errors:
            logger.error(
                "startup_recovery_invariants_failed",
                extra={"extra": {"errors": invariant_errors, "cycle_id": cycle_id}},
            )

        logger.info(
            "startup_recovery_completed",
            extra={
                "extra": {
                    "cycle_id": cycle_id,
                    "fills_inserted": fills_inserted,
                    "invariant_error_count": len(invariant_errors),
                    "ts_utc": datetime.now(UTC).isoformat(),
                }
            },
        )
        return StartupRecoveryResult(
            observe_only_required=bool(invariant_errors),
            invariant_errors=tuple(invariant_errors),
            fills_inserted=fills_inserted,
        )
