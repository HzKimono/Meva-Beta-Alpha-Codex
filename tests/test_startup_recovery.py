from __future__ import annotations

from dataclasses import dataclass, field
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
    refresh_calls: int = 0
    seen_mark_prices: dict[str, Decimal] = field(default_factory=dict)

    def refresh(self, symbols: list[str], mark_prices: dict[str, Decimal]) -> int:
        assert symbols
        self.refresh_calls += 1
        self.seen_mark_prices = dict(mark_prices)
        return self.fills_inserted

    def get_positions(self) -> list[Position]:
        return self.positions


@dataclass
class _StubPortfolioService:
    balances: list[Balance]

    def get_balances(self) -> list[Balance]:
        return self.balances


def _position(symbol: str, qty: str = "1") -> Position:
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        avg_cost=Decimal("100"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        fees_paid=Decimal("0"),
    )


def test_run_with_prices_calls_refresh_and_runs_invariants() -> None:
    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(
        fills_inserted=2,
        positions=[_position("BTCTRY")],
    )
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=1000.0)])

    result = service.run(
        cycle_id="cycle-1",
        symbols=["BTCTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices={"BTCTRY": Decimal("123.45")},
    )

    assert execution.calls == 1
    assert accounting.refresh_calls == 1
    assert accounting.seen_mark_prices == {"BTCTRY": Decimal("123.45")}
    assert result.observe_only_required is False
    assert result.observe_only_reason is None
    assert result.fills_inserted == 2


def test_run_without_prices_forces_observe_only_and_skips_refresh() -> None:
    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(fills_inserted=7, positions=[_position("BTCTRY")])
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=1000.0)])

    result = service.run(
        cycle_id="cycle-2",
        symbols=["BTCTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices=None,
    )

    assert execution.calls == 1
    assert accounting.refresh_calls == 0
    assert result.observe_only_required is True
    assert result.observe_only_reason == "missing_mark_prices"
    assert result.fills_inserted == 0


def test_startup_recovery_is_idempotent() -> None:
    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(fills_inserted=0, positions=[])
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=50.0)])
    prices = {"ETHTRY": Decimal("2000")}

    first = service.run(
        cycle_id="cycle-a",
        symbols=["ETHTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices=prices,
    )
    second = service.run(
        cycle_id="cycle-b",
        symbols=["ETHTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices=prices,
    )

    assert first.observe_only_required == second.observe_only_required
    assert first.observe_only_reason == second.observe_only_reason is None
    assert first.invariant_errors == second.invariant_errors == ()
    assert first.fills_inserted == second.fills_inserted == 0


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
        mark_prices={"BTCTRY": Decimal("100")},
    )

    assert result.observe_only_required is True
    assert result.observe_only_reason is None
    assert "negative_balance:TRY" in result.invariant_errors
