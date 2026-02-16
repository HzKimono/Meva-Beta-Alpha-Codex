from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.accounting import Position
from btcbot.domain.models import Balance
from btcbot.services.startup_recovery import StartupRecoveryService


@dataclass
class _StubExecutionService:
    calls: int = 0

    def refresh_order_lifecycle(self, symbols: list[str]) -> None:
        assert symbols
        self.calls += 1


@dataclass
class _StubAccountingService:
    fills_inserted: int
    positions: list[Position]

    def refresh(self, symbols: list[str], mark_prices: dict[str, Decimal]) -> int:
        assert symbols
        assert mark_prices == {}
        return self.fills_inserted

    def get_positions(self) -> list[Position]:
        return self.positions


@dataclass
class _StubPortfolioService:
    balances: list[Balance]

    def get_balances(self) -> list[Balance]:
        return self.balances


def test_startup_recovery_reconciles_and_converges() -> None:
    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(
        fills_inserted=2,
        positions=[
            Position(
                symbol="BTCTRY",
                qty=Decimal("1"),
                avg_cost=Decimal("100"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                fees_paid=Decimal("0"),
            )
        ],
    )
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=1000.0)])

    result = service.run(
        cycle_id="cycle-1",
        symbols=["BTCTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
    )

    assert execution.calls == 1
    assert result.observe_only_required is False
    assert result.fills_inserted == 2


def test_startup_recovery_is_idempotent() -> None:
    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(fills_inserted=0, positions=[])
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=50.0)])

    first = service.run(
        cycle_id="cycle-a",
        symbols=["ETHTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
    )
    second = service.run(
        cycle_id="cycle-b",
        symbols=["ETHTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
    )

    assert first.observe_only_required == second.observe_only_required
    assert first.invariant_errors == second.invariant_errors == ()


def test_startup_recovery_forces_observe_only_on_invariant_failure() -> None:
    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(fills_inserted=0, positions=[])
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=-1.0)])

    result = service.run(
        cycle_id="cycle-err",
        symbols=["BTCTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
    )

    assert result.observe_only_required is True
    assert "negative_balance:TRY" in result.invariant_errors
