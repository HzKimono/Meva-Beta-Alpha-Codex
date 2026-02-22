from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from btcbot.domain.money_policy import (
    DEFAULT_MONEY_POLICY,
    MoneyMathPolicy,
    round_qty,
    round_quote,
)

def quantize_money(value: Decimal, policy: MoneyMathPolicy | None = None) -> Decimal:
    """Centralized quote normalization to avoid execution/accounting drift."""

    effective_policy = policy or DEFAULT_MONEY_POLICY
    return round_quote(value, effective_policy)


def quantize_qty(value: Decimal, policy: MoneyMathPolicy | None = None) -> Decimal:
    effective_policy = policy or DEFAULT_MONEY_POLICY
    return round_qty(value, effective_policy)


class AccountingEventType(StrEnum):
    FILL_RECORDED = "FILL_RECORDED"
    FEE_RECORDED = "FEE_RECORDED"
    FUNDING_COST_RECORDED = "FUNDING_COST_RECORDED"
    SLIPPAGE_RECORDED = "SLIPPAGE_RECORDED"
    TRANSFER = "TRANSFER"
    REBALANCE = "REBALANCE"
    WITHDRAWAL = "WITHDRAWAL"


@dataclass(frozen=True)
class AccountingLedgerEvent:
    event_id: str
    ts: datetime
    type: AccountingEventType
    symbol: str | None
    side: str | None
    qty: Decimal
    price_try: Decimal | None
    amount_try: Decimal | None
    fee_currency: str | None
    reference_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical_ts(self) -> datetime:
        if self.ts.tzinfo is None:
            return self.ts.replace(tzinfo=UTC)
        return self.ts.astimezone(UTC)


@dataclass(frozen=True)
class PositionLot:
    qty: Decimal
    unit_cost_try: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class SymbolPnlState:
    symbol: str
    qty: Decimal = Decimal("0")
    avg_cost_try: Decimal = Decimal("0")
    realized_pnl_try: Decimal = Decimal("0")
    unrealized_pnl_try: Decimal = Decimal("0")
    fees_try: Decimal = Decimal("0")
    funding_cost_try: Decimal = Decimal("0")
    slippage_try: Decimal = Decimal("0")


@dataclass(frozen=True)
class PortfolioAccountingState:
    as_of: datetime
    balances_try: dict[str, Decimal]
    locked_try: dict[str, Decimal]
    treasury_try: Decimal
    trading_capital_try: Decimal
    realized_pnl_try: Decimal
    unrealized_pnl_try: Decimal
    fees_try: Decimal
    funding_cost_try: Decimal
    slippage_try: Decimal
    symbols: dict[str, SymbolPnlState]

    @property
    def equity_try(self) -> Decimal:
        return quantize_money(
            self.trading_capital_try
            + self.treasury_try
            + self.realized_pnl_try
            + self.unrealized_pnl_try
            - self.fees_try
            - self.funding_cost_try
            - self.slippage_try
        )
