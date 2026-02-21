from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.ledger import (
    EquityPoint,
    LedgerEvent,
    LedgerEventType,
    LedgerSnapshot,
    LedgerState,
    apply_events,
    compute_max_drawdown,
    compute_realized_pnl,
    compute_unrealized_pnl,
)
from btcbot.domain.models import normalize_symbol
from btcbot.domain.stage4 import Fill, LifecycleAction, LifecycleActionType
from btcbot.ports_price_conversion import FeeConversionRateError, PriceConverter
from btcbot.services.state_store import StateStore


@dataclass(frozen=True)
class LedgerIngestResult:
    events_attempted: int
    events_inserted: int
    events_ignored: int


@dataclass(frozen=True)
class SymbolPnlBreakdown:
    symbol: str
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    position_qty: Decimal


@dataclass(frozen=True)
class PnlReport:
    realized_pnl_total: Decimal
    unrealized_pnl_total: Decimal
    fees_total_by_currency: dict[str, Decimal]
    per_symbol: list[SymbolPnlBreakdown]
    equity_estimate: Decimal
    fees_total_try: Decimal | None = None
    fee_conversion_missing_currencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimulatedFill:
    event: LedgerEvent
    fee_event: LedgerEvent
    applied_price: Decimal
    baseline_price: Decimal


@dataclass(frozen=True)
class FinancialBreakdown:
    cash_try: Decimal
    position_mtm_try: Decimal
    realized_pnl_try: Decimal
    unrealized_pnl_try: Decimal
    fees_try: Decimal
    slippage_try: Decimal
    gross_pnl_try: Decimal
    net_pnl_try: Decimal
    equity_try: Decimal
    turnover_try: Decimal


@dataclass(frozen=True)
class LedgerCheckpoint:
    event_count: int
    last_ts: datetime | None
    last_event_id: str | None

    @property
    def checkpoint_id(self) -> str:
        last_ts = self.last_ts.isoformat() if self.last_ts is not None else "none"
        last_event = self.last_event_id if self.last_event_id is not None else "none"
        return f"{self.event_count}:{last_ts}:{last_event}"


class LedgerService:
    def __init__(self, state_store: StateStore, logger: logging.Logger) -> None:
        self.state_store = state_store
        self.logger = logger
        self._cached_state: LedgerState | None = None
        self._cached_event_count = 0
        self._cached_last_event_id: str | None = None
        self.last_reduce_delta_events = 0

    def ingest_exchange_updates(self, fills: list[Fill]) -> LedgerIngestResult:
        events: list[LedgerEvent] = []
        for fill in fills:
            symbol = normalize_symbol(fill.symbol)
            fill_event_id = f"fill:{fill.fill_id}"
            events.append(
                LedgerEvent(
                    event_id=fill_event_id,
                    ts=fill.ts.astimezone(UTC),
                    symbol=symbol,
                    type=LedgerEventType.FILL,
                    side=fill.side.upper(),
                    qty=fill.qty,
                    price=fill.price,
                    fee=None,
                    fee_currency=None,
                    exchange_trade_id=fill.fill_id,
                    exchange_order_id=fill.order_id,
                    client_order_id=None,
                    meta={"source": "stage4_accounting_fill"},
                )
            )
            if fill.fee > 0:
                events.append(
                    LedgerEvent(
                        event_id=f"fee:{fill.fill_id}",
                        ts=fill.ts.astimezone(UTC),
                        symbol=symbol,
                        type=LedgerEventType.FEE,
                        side=None,
                        qty=Decimal("0"),
                        price=None,
                        fee=fill.fee,
                        fee_currency=fill.fee_asset.upper(),
                        exchange_trade_id=f"fee:{fill.fill_id}",
                        exchange_order_id=fill.order_id,
                        client_order_id=None,
                        meta={
                            "linked_fill_id": fill.fill_id,
                            "source": "stage4_accounting_fill_fee",
                        },
                    )
                )

        append = self.state_store.append_ledger_events(events)
        return LedgerIngestResult(
            events_attempted=append.attempted,
            events_inserted=append.inserted,
            events_ignored=append.ignored,
        )

    def simulate_dry_run_fills(
        self,
        *,
        cycle_id: str,
        actions: list[LifecycleAction],
        mark_prices: dict[str, Decimal],
        slippage_bps: Decimal,
        fees_bps: Decimal,
        ts: datetime,
        fee_currency: str = "TRY",
        fee_currency_to_try_rate: Decimal | None = None,
    ) -> list[SimulatedFill]:
        simulated: list[SimulatedFill] = []
        for idx, action in enumerate(actions):
            if action.action_type != LifecycleActionType.SUBMIT:
                continue
            symbol = normalize_symbol(action.symbol)
            baseline = mark_prices.get(symbol)
            if baseline is None or baseline <= 0:
                continue
            sign = Decimal("1") if action.side.upper() == "BUY" else Decimal("-1")
            slip_mult = Decimal("1") + sign * (slippage_bps / Decimal("10000"))
            applied = baseline * slip_mult
            notional = applied * action.qty
            fee_try = notional * (fees_bps / Decimal("10000"))
            fee_ccy = fee_currency.upper()
            fee_amount = fee_try
            if fee_ccy != "TRY" and fee_currency_to_try_rate is not None and fee_currency_to_try_rate > 0:
                fee_amount = fee_try / fee_currency_to_try_rate
            id_component = action.client_order_id or action.exchange_order_id or str(idx)
            fill_id = f"s7:{cycle_id}:{id_component}"
            fill_event = LedgerEvent(
                event_id=f"fill:{fill_id}",
                ts=ts.astimezone(UTC),
                symbol=symbol,
                type=LedgerEventType.FILL,
                side=action.side.upper(),
                qty=action.qty,
                price=applied,
                fee=None,
                fee_currency=None,
                exchange_trade_id=fill_id,
                exchange_order_id=action.exchange_order_id,
                client_order_id=action.client_order_id,
                meta={"source": "stage7_dry_run", "baseline_price": str(baseline)},
            )
            fee_event = LedgerEvent(
                event_id=f"fee:{fill_id}",
                ts=ts.astimezone(UTC),
                symbol=symbol,
                type=LedgerEventType.FEE,
                side=None,
                qty=Decimal("0"),
                price=None,
                fee=fee_amount,
                fee_currency=fee_ccy,
                exchange_trade_id=f"fee:{fill_id}",
                exchange_order_id=action.exchange_order_id,
                client_order_id=action.client_order_id,
                meta={"source": "stage7_dry_run", "linked_fill_id": fill_id},
            )
            simulated.append(
                SimulatedFill(
                    event=fill_event,
                    fee_event=fee_event,
                    applied_price=applied,
                    baseline_price=baseline,
                )
            )
        return simulated

    def append_simulated_fills(self, fills: list[SimulatedFill]) -> LedgerIngestResult:
        events: list[LedgerEvent] = []
        for fill in fills:
            events.append(fill.event)
            events.append(fill.fee_event)
        append = self.state_store.append_ledger_events(events)
        return LedgerIngestResult(
            events_attempted=append.attempted,
            events_inserted=append.inserted,
            events_ignored=append.ignored,
        )

    def _position_mtm(self, state: LedgerState, mark_prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for symbol, ledger in state.symbols.items():
            mark = mark_prices.get(symbol)
            if mark is None:
                continue
            total += sum((lot.qty * mark for lot in ledger.lots), Decimal("0"))
        return total

    def financial_breakdown(
        self,
        *,
        mark_prices: dict[str, Decimal],
        cash_try: Decimal,
        price_for_fee_conversion: PriceConverter | None = None,
        slippage_try: Decimal = Decimal("0"),
    ) -> FinancialBreakdown:
        events, state = self._load_state_incremental()
        realized = compute_realized_pnl(state)
        normalized_marks = {
            normalize_symbol(symbol): value for symbol, value in mark_prices.items()
        }
        unrealized = compute_unrealized_pnl(state, normalized_marks)

        fees_try, _ = self._compute_fees_try(
            fees_by_currency=state.fees_by_currency,
            price_for_fee_conversion=price_for_fee_conversion,
        )

        turnover = Decimal("0")
        for event in events:
            if event.type == LedgerEventType.FILL and event.price is not None:
                turnover += abs(event.price * event.qty)

        gross = realized + unrealized
        net = gross - fees_try - slippage_try
        mtm = self._position_mtm(state, normalized_marks)
        equity = cash_try + mtm

        return FinancialBreakdown(
            cash_try=cash_try,
            position_mtm_try=mtm,
            realized_pnl_try=realized,
            unrealized_pnl_try=unrealized,
            fees_try=fees_try,
            slippage_try=slippage_try,
            gross_pnl_try=gross,
            net_pnl_try=net,
            equity_try=equity,
            turnover_try=turnover,
        )

    def snapshot(
        self,
        *,
        mark_prices: dict[str, Decimal],
        cash_try: Decimal,
        price_for_fee_conversion: PriceConverter | None = None,
        slippage_try: Decimal = Decimal("0"),
        ts: datetime | None = None,
    ) -> LedgerSnapshot:
        breakdown = self.financial_breakdown(
            mark_prices=mark_prices,
            cash_try=cash_try,
            price_for_fee_conversion=price_for_fee_conversion,
            slippage_try=slippage_try,
        )

        with self.state_store._connect() as conn:
            rows = conn.execute(
                "SELECT ts,total_equity_try FROM pnl_snapshots ORDER BY ts"
            ).fetchall()
        points = [
            EquityPoint(
                ts=datetime.fromisoformat(str(row["ts"])),
                equity_try=Decimal(str(row["total_equity_try"])),
            )
            for row in rows
        ]
        if ts is not None:
            points.append(EquityPoint(ts=ts, equity_try=breakdown.equity_try))

        return LedgerSnapshot(
            cash_try=breakdown.cash_try,
            position_mtm_try=breakdown.position_mtm_try,
            gross_pnl_try=breakdown.gross_pnl_try,
            realized_pnl_try=breakdown.realized_pnl_try,
            unrealized_pnl_try=breakdown.unrealized_pnl_try,
            net_pnl_try=breakdown.net_pnl_try,
            fees_try=breakdown.fees_try,
            slippage_try=breakdown.slippage_try,
            turnover_try=breakdown.turnover_try,
            equity_try=breakdown.equity_try,
            max_drawdown=compute_max_drawdown(points) if points else Decimal("0"),
        )

    def report(
        self,
        mark_prices: dict[str, Decimal],
        cash_try: Decimal = Decimal("0"),
        price_for_fee_conversion: PriceConverter | None = None,
    ) -> PnlReport:
        events, state = self._load_state_incremental()

        normalized_marks = {
            normalize_symbol(symbol): value for symbol, value in mark_prices.items()
        }
        realized = compute_realized_pnl(state)
        unrealized = compute_unrealized_pnl(state, normalized_marks)
        fees_total_try, missing_currencies = self._compute_fees_try(
            fees_by_currency=state.fees_by_currency,
            price_for_fee_conversion=price_for_fee_conversion,
        )
        mtm = self._position_mtm(state, normalized_marks)
        equity_estimate = cash_try + mtm

        per_symbol: list[SymbolPnlBreakdown] = []
        for symbol, ledger in sorted(state.symbols.items()):
            qty = sum((lot.qty for lot in ledger.lots), Decimal("0"))
            symbol_unrealized = Decimal("0")
            mark = normalized_marks.get(symbol)
            if mark is not None:
                symbol_unrealized = sum(
                    ((mark - lot.unit_cost) * lot.qty for lot in ledger.lots), Decimal("0")
                )
            per_symbol.append(
                SymbolPnlBreakdown(
                    symbol=symbol,
                    realized_pnl=ledger.realized_pnl,
                    unrealized_pnl=symbol_unrealized,
                    position_qty=qty,
                )
            )

        return PnlReport(
            realized_pnl_total=realized,
            unrealized_pnl_total=unrealized,
            fees_total_by_currency=dict(state.fees_by_currency),
            per_symbol=per_symbol,
            equity_estimate=equity_estimate,
            fees_total_try=fees_total_try,
            fee_conversion_missing_currencies=tuple(sorted(missing_currencies)),
        )


    def _compute_fees_try(
        self,
        *,
        fees_by_currency: dict[str, Decimal],
        price_for_fee_conversion: Callable[[str, str], Decimal] | None,
    ) -> tuple[Decimal, set[str]]:
        fees_try = Decimal("0")
        missing_rates: set[str] = set()
        for ccy, amount in fees_by_currency.items():
            normalized = ccy.upper()
            if normalized == "TRY":
                fees_try += amount
                continue
            if price_for_fee_conversion is None:
                missing_rates.add(normalized)
                continue
            try:
                converted = price_for_fee_conversion(normalized, "TRY")
            except FeeConversionRateError:
                missing_rates.add(normalized)
                continue
            fees_try += amount * Decimal(str(converted))
        return fees_try, missing_rates

    def _load_state_incremental(self) -> tuple[list[LedgerEvent], LedgerState]:
        events = self.state_store.load_ledger_events()
        if not events:
            self._cached_state = LedgerState()
            self._cached_event_count = 0
            self._cached_last_event_id = None
            self.last_reduce_delta_events = 0
            return events, LedgerState()

        if (
            self._cached_state is None
            or self._cached_event_count > len(events)
            or (
                self._cached_event_count > 0
                and self._cached_event_count <= len(events)
                and self._cached_last_event_id
                != events[self._cached_event_count - 1].event_id
            )
        ):
            state = apply_events(LedgerState(), events)
            self.last_reduce_delta_events = len(events)
        else:
            delta = events[self._cached_event_count :]
            if delta:
                state = apply_events(self._cached_state, delta)
            else:
                state = self._cached_state
            self.last_reduce_delta_events = len(delta)

        self._cached_state = state
        self._cached_event_count = len(events)
        self._cached_last_event_id = events[-1].event_id
        return events, state

    def checkpoint(self) -> LedgerCheckpoint:
        events, _ = self._load_state_incremental()
        if not events:
            return LedgerCheckpoint(event_count=0, last_ts=None, last_event_id=None)
        last = events[-1]
        return LedgerCheckpoint(
            event_count=len(events),
            last_ts=last.ts,
            last_event_id=last.event_id,
        )
