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
    recovered_reason: str | None = None


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
        do_refresh_lifecycle: bool = False,
        state_store: object | None = None,
    ) -> StartupRecoveryResult:
        logger.info("startup_recovery_started", extra={"extra": {"cycle_id": cycle_id}})

        normalized_symbols = [str(symbol) for symbol in symbols]
        refresh_order_lifecycle = getattr(execution_service, "refresh_order_lifecycle", None)
        if do_refresh_lifecycle and callable(refresh_order_lifecycle):
            refresh_order_lifecycle(normalized_symbols)
            mark_lifecycle_refreshed = getattr(execution_service, "mark_lifecycle_refreshed", None)
            if callable(mark_lifecycle_refreshed):
                mark_lifecycle_refreshed(cycle_id=cycle_id)

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
        prime_cycle_balances = getattr(execution_service, "prime_cycle_balances", None)
        if callable(prime_cycle_balances):
            prime_cycle_balances(cycle_id=cycle_id, balances=balances)
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

        recovered_reason: str | None = None
        if state_store is not None:
            list_open_replace_txs = getattr(state_store, "list_open_replace_txs", None)
            if callable(list_open_replace_txs):
                open_replace_txs = list_open_replace_txs()
                if open_replace_txs:
                    recovered_reason = "open_replace_transactions_detected"
                    logger.warning(
                        "startup_recovery_partial_state_detected",
                        extra={
                            "extra": {
                                "cycle_id": cycle_id,
                                "open_replace_tx_count": len(open_replace_txs),
                                "recovered_reason": recovered_reason,
                            }
                        },
                    )

        logger.info(
            "startup_recovery_completed",
            extra={
                "extra": {
                    "cycle_id": cycle_id,
                    "observe_only_reason": observe_only_reason,
                    "fills_inserted": fills_inserted,
                    "invariant_error_count": len(invariant_errors),
                    "recovered_reason": recovered_reason,
                    "ts_utc": datetime.now(UTC).isoformat(),
                }
            },
        )
        return StartupRecoveryResult(
            observe_only_required=bool(invariant_errors) or observe_only_reason is not None,
            observe_only_reason=observe_only_reason,
            invariant_errors=tuple(invariant_errors),
            fills_inserted=fills_inserted,
            recovered_reason=recovered_reason,
        )
