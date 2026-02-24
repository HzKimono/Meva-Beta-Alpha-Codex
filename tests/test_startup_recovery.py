from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.accounting import Position
from btcbot.domain.models import (
    Balance,
    ExchangeOrderStatus,
    OpenOrderItem,
    OpenOrders,
    Order,
    OrderIntent,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    PairInfo,
    SymbolRules,
)
from btcbot.services.execution_service import ExecutionService
from btcbot.services.startup_recovery import StartupRecoveryService
from btcbot.services.state_store import StateStore


@dataclass
class _StubExecutionService:
    calls: int = 0
    primed: int = 0
    marked: int = 0

    def refresh_order_lifecycle(self, symbols: list[str]) -> None:
        assert symbols
        self.calls += 1

    def mark_lifecycle_refreshed(self, *, cycle_id: str) -> None:
        assert cycle_id
        self.marked += 1

    def prime_cycle_balances(self, *, cycle_id: str, balances: list[Balance]) -> None:
        assert cycle_id
        assert balances
        self.primed += 1


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

    assert execution.calls == 0
    assert execution.marked == 0
    assert execution.primed == 1
    assert accounting.refresh_calls == 1
    assert accounting.seen_mark_prices == {"BTCTRY": Decimal("123.45")}
    assert result.observe_only_required is False
    assert result.observe_only_reason is None
    assert result.fills_inserted == 2


def test_run_with_do_refresh_lifecycle_true_calls_refresh_and_marks() -> None:
    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(
        fills_inserted=0,
        positions=[_position("BTCTRY")],
    )
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=1000.0)])

    _ = service.run(
        cycle_id="cycle-refresh",
        symbols=["BTCTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices={"BTCTRY": Decimal("123.45")},
        do_refresh_lifecycle=True,
    )

    assert execution.calls == 1
    assert execution.marked == 1
    assert execution.primed == 1


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

    assert execution.calls == 0
    assert execution.marked == 0
    assert execution.primed == 1
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


class _StartupRecoveryExchange(ExchangeClient):
    def __init__(self, snapshots: list[OrderSnapshot]) -> None:
        self.snapshots = snapshots
        self.get_balances_calls = 0
        self.get_open_orders_calls = 0
        self.place_calls = 0

    def get_balances(self) -> list[Balance]:
        self.get_balances_calls += 1
        return [Balance(asset="TRY", free=1000)]

    def get_orderbook(self, symbol: str, limit: int | None = None) -> tuple[float, float]:
        del symbol, limit
        return (0.0, 0.0)

    def get_exchange_info(self) -> list[PairInfo]:
        return []

    def get_open_orders(self, pair_symbol: str) -> OpenOrders:
        self.get_open_orders_calls += 1
        bids: list[OpenOrderItem] = []
        for snapshot in self.snapshots:
            if snapshot.pair_symbol != pair_symbol:
                continue
            bids.append(
                OpenOrderItem(
                    id=int(snapshot.order_id),
                    price=snapshot.price,
                    amount=snapshot.quantity,
                    quantity=snapshot.quantity,
                    pairSymbol=snapshot.pair_symbol,
                    pairSymbolNormalized=snapshot.pair_symbol,
                    type="limit",
                    method="buy",
                    orderClientId=snapshot.client_order_id,
                    time=snapshot.timestamp,
                    updateTime=snapshot.update_time,
                    status="Untouched",
                )
            )
        return OpenOrders(bids=bids, asks=[])

    def get_all_orders(self, pair_symbol: str, start_ms: int, end_ms: int) -> list[OrderSnapshot]:
        del start_ms, end_ms
        return [item for item in self.snapshots if item.pair_symbol == pair_symbol]

    def get_order(self, order_id: str) -> OrderSnapshot:
        raise ValueError(order_id)

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        client_order_id: str | None = None,
    ) -> Order:
        self.place_calls += 1
        return Order(
            order_id=f"oid-{self.place_calls}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            status=OrderStatus.NEW,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def cancel_order(self, order_id: str) -> bool:
        del order_id
        return False

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        del symbol
        return []


class _StubMarketDataService:
    def get_symbol_rules(self, pair_symbol: str) -> SymbolRules:
        return SymbolRules(pair_symbol=pair_symbol, price_scale=2, quantity_scale=4)


def test_startup_recovery_imports_exchange_open_and_closes_local_only(tmp_path) -> None:
    state_store = StateStore(str(tmp_path / "state.db"))
    now = datetime.now(UTC)
    state_store.save_order(
        Order(
            order_id="9001",
            client_order_id="cid-local-only",
            symbol="ADATRY",
            side=OrderSide.BUY,
            price=10,
            quantity=1,
            status=OrderStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )

    exchange = _StartupRecoveryExchange(
        snapshots=[
            OrderSnapshot(
                order_id="1001",
                client_order_id="cid-external",
                pair_symbol="ADATRY",
                side=OrderSide.BUY,
                price=10,
                quantity=1,
                status=ExchangeOrderStatus.OPEN,
                timestamp=1700000000000,
                update_time=1700000000100,
            )
        ]
    )
    execution_service = ExecutionService(
        exchange=exchange,
        state_store=state_store,
        market_data_service=_StubMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    service = StartupRecoveryService()

    accounting = _StubAccountingService(fills_inserted=0, positions=[_position("ADATRY")])
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=1000.0)])

    _ = service.run(
        cycle_id="cycle-startup",
        symbols=["ADATRY"],
        execution_service=execution_service,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices={"ADATRY": Decimal("10")},
        do_refresh_lifecycle=True,
    )

    closed_local = state_store.get_order("9001")
    imported = state_store.get_order("1001")
    assert closed_local is not None
    assert imported is not None
    assert closed_local.status == OrderStatus.REJECTED
    assert imported.status == OrderStatus.OPEN

    with state_store._connect() as conn:
        imported_row = conn.execute(
            "SELECT exchange_status_raw, reconciled FROM orders WHERE order_id = ?",
            ("1001",),
        ).fetchone()
        missing_row = conn.execute(
            "SELECT exchange_status_raw, status, reconciled FROM orders WHERE order_id = ?",
            ("9001",),
        ).fetchone()

    assert imported_row is not None
    assert imported_row["exchange_status_raw"] == "external_open:exchange_reconcile"
    assert imported_row["reconciled"] == 1

    assert missing_row is not None
    assert missing_row["status"] == "rejected"
    assert str(missing_row["exchange_status_raw"]).startswith("missing_on_exchange_")
    assert missing_row["reconciled"] == 1

    summary = execution_service.last_lifecycle_refresh_summary
    assert summary["imported_external_open"] == 1
    assert summary["marked_missing"] == 1
    assert summary["closed"] == 1


def test_startup_recovery_and_execute_same_cycle_reuses_refresh_and_balances(tmp_path) -> None:
    state_store = StateStore(str(tmp_path / "state.db"))
    exchange = _StartupRecoveryExchange(snapshots=[])
    execution_service = ExecutionService(
        exchange=exchange,
        state_store=state_store,
        market_data_service=_StubMarketDataService(),
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )
    service = StartupRecoveryService()

    accounting = _StubAccountingService(fills_inserted=0, positions=[_position("BTCTRY")])
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=1000.0)])

    service.run(
        cycle_id="cycle-shared",
        symbols=["BTCTRY"],
        execution_service=execution_service,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices={"BTCTRY": Decimal("100")},
    )

    placed = execution_service.execute_intents(
        [
            OrderIntent(
                symbol="BTC_TRY",
                side=OrderSide.BUY,
                price=100.0,
                quantity=0.1,
                notional=10.0,
                cycle_id="cycle-shared",
            )
        ]
    )

    assert placed == 1
    assert exchange.get_balances_calls == 0
    assert exchange.get_open_orders_calls == 1


def test_startup_recovery_marks_partial_replace_state_as_recovered() -> None:
    class _StubStateStore:
        def list_open_replace_txs(self):
            return [object()]

    service = StartupRecoveryService()
    execution = _StubExecutionService()
    accounting = _StubAccountingService(fills_inserted=0, positions=[])
    portfolio = _StubPortfolioService(balances=[Balance(asset="TRY", free=1.0)])

    result = service.run(
        cycle_id="cycle-partial",
        symbols=["BTCTRY"],
        execution_service=execution,
        accounting_service=accounting,
        portfolio_service=portfolio,
        mark_prices={"BTCTRY": Decimal("1")},
        state_store=_StubStateStore(),
    )

    assert result.recovered_reason == "open_replace_transactions_detected"
