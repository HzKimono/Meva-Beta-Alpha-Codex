from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
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
    observe_only_reason: str | None
    invariant_errors: tuple[str, ...]
    fills_inserted: int


class StartupRecoveryService:
    def run(
        self,
        *,
        cycle_id: str,
        symbols: Sequence[str],
        execution_service: ExecutionService,
        accounting_service: AccountingService,
        portfolio_service: PortfolioService,
        mark_prices: Mapping[str, Decimal] | None = None,
    ) -> StartupRecoveryResult:
        logger.info("startup_recovery_started", extra={"extra": {"cycle_id": cycle_id}})

        normalized_symbols = [str(symbol) for symbol in symbols]
        refresh_order_lifecycle = getattr(execution_service, "refresh_order_lifecycle", None)
        if callable(refresh_order_lifecycle):
            refresh_order_lifecycle(normalized_symbols)

        mark_prices_dict = {
            str(symbol): Decimal(str(price)) for symbol, price in (mark_prices or {}).items()
        }
        missing_symbols = [
            symbol for symbol in normalized_symbols if symbol not in mark_prices_dict
        ]

        observe_only_reason: str | None = None
        if mark_prices is None or missing_symbols:
            observe_only_reason = "missing_mark_prices"
            logger.warning(
                "startup_recovery_missing_mark_prices",
                extra={
                    "extra": {
                        "cycle_id": cycle_id,
                        "missing_symbols": missing_symbols,
                    }
                },
            )
            fills_inserted = 0
        else:
            fills_inserted = accounting_service.refresh(
                normalized_symbols, mark_prices=mark_prices_dict
            )

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
                    "observe_only_reason": observe_only_reason,
                    "fills_inserted": fills_inserted,
                    "invariant_error_count": len(invariant_errors),
                    "ts_utc": datetime.now(UTC).isoformat(),
                }
            },
        )
        return StartupRecoveryResult(
            observe_only_required=bool(invariant_errors) or observe_only_reason is not None,
            observe_only_reason=observe_only_reason,
            invariant_errors=tuple(invariant_errors),
            fills_inserted=fills_inserted,
        )
