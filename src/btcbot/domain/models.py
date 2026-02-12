from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from btcbot.domain.symbols import canonical_symbol


class ValidationError(ValueError):
    """Raised when an order candidate violates symbol rules."""


def parse_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        return Decimal(normalized)
    raise TypeError(f"Cannot parse decimal from {type(value)!r}")


def normalize_symbol(symbol: str) -> str:
    return canonical_symbol(symbol)


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    NEW = "new"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ExchangeOrderStatus(StrEnum):
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ReconcileStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


class Balance(BaseModel):
    asset: str
    free: float
    locked: float = 0.0


class SymbolInfo(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str = "TRY"
    min_notional: float | None = None
    step_size: float | None = None
    tick_size: float | None = None


class SymbolRules(BaseModel):
    pair_symbol: str
    price_scale: int
    quantity_scale: int
    min_total: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    tick_size: Decimal | None = None
    step_size: Decimal | None = None


class PairInfo(BaseModel):
    pair_symbol: str = Field(alias="pairSymbol")
    name: str | None = None
    name_normalized: str | None = Field(default=None, alias="nameNormalized")
    status: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    numerator_scale: int = Field(alias="numeratorScale")
    denominator_scale: int = Field(alias="denominatorScale")
    min_total_amount: Decimal | None = Field(default=None, alias="minTotalAmount")
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_quantity: Decimal | None = Field(default=None, alias="minQuantity")
    max_quantity: Decimal | None = Field(default=None, alias="maxQuantity")
    tick_size: Decimal | None = Field(default=None, alias="tickSize")
    step_size: Decimal | None = Field(default=None, alias="stepSize")


class Order(BaseModel):
    order_id: str
    client_order_id: str | None = None
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    status: OrderStatus = OrderStatus.NEW
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OrderIntent(BaseModel):
    symbol: str
    side: OrderSide = OrderSide.BUY
    price: float
    quantity: float
    notional: float
    cycle_id: str


class ExchangeError(RuntimeError):
    """Raised when an exchange request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: int | str | None = None,
        error_message: str | None = None,
        request_path: str | None = None,
        request_method: str | None = None,
        request_params: dict[str, object] | None = None,
        request_json: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        self.request_path = request_path
        self.request_method = request_method
        self.request_params = request_params
        self.request_json = request_json


class SubmitOrderRequest(BaseModel):
    pair_symbol: str
    price: Decimal
    quantity: Decimal
    side: OrderSide
    client_order_id: str


class SubmitOrderResult(BaseModel):
    order_id: str
    success: bool = True


class CancelOrderResult(BaseModel):
    success: bool


def make_client_order_id(intent: OrderIntent, *, prefix: str = "meva2") -> str:
    cycle = re.sub(r"[^A-Za-z0-9_-]", "", intent.cycle_id)[:8] or "cycle"
    symbol = re.sub(r"[^A-Za-z0-9]", "", intent.symbol).upper()[:8] or "SYMB"
    side_short = "b" if intent.side == OrderSide.BUY else "s"
    raw = (
        f"{symbol}|{intent.side.value}|{intent.price:.8f}|"
        f"{intent.quantity:.8f}|{intent.notional:.8f}|{intent.cycle_id}"
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    value = f"{prefix}-{cycle}-{symbol}-{side_short}-{digest}"
    return value[:40]


class BtcturkBalanceItem(BaseModel):
    asset: str
    balance: Decimal
    locked: Decimal
    free: Decimal
    order_fund: Decimal | None = Field(default=None, alias="orderFund")
    request_fund: Decimal | None = Field(default=None, alias="requestFund")
    precision: int | None = None
    timestamp: int | None = None
    asset_name: str | None = Field(default=None, alias="assetname")


class OpenOrderItem(BaseModel):
    id: int
    price: Decimal
    amount: Decimal
    quantity: Decimal
    stop_price: Decimal | None = Field(default=None, alias="stopPrice")
    pair_symbol: str = Field(alias="pairSymbol")
    pair_symbol_normalized: str = Field(alias="pairSymbolNormalized")
    type: str
    method: str
    order_client_id: str | None = Field(default=None, alias="orderClientId")
    time: int
    update_time: int | None = Field(default=None, alias="updateTime")
    status: str
    left_amount: Decimal | None = Field(default=None, alias="leftAmount")


class OpenOrders(BaseModel):
    bids: list[OpenOrderItem]
    asks: list[OpenOrderItem]


class OrderSnapshot(BaseModel):
    order_id: str
    client_order_id: str | None = None
    pair_symbol: str
    side: OrderSide | None = None
    price: Decimal
    quantity: Decimal
    status: ExchangeOrderStatus = ExchangeOrderStatus.UNKNOWN
    timestamp: int
    update_time: int | None = None
    status_raw: str | None = None


class ReconcileOutcome(BaseModel):
    status: ReconcileStatus
    order_id: str | None = None
    reason: str


BtcturkOpenOrderItem = OpenOrderItem
BtcturkOpenOrders = OpenOrders


def match_order_by_client_id(
    snapshots: list[OrderSnapshot], client_order_id: str
) -> OrderSnapshot | None:
    for item in snapshots:
        if item.client_order_id == client_order_id:
            return item
    return None


def fallback_match_by_fields(
    snapshots: list[OrderSnapshot],
    pair_symbol: str,
    side: OrderSide | None,
    price: Decimal,
    quantity: Decimal,
    price_tolerance: Decimal,
    qty_tolerance: Decimal,
    time_window: tuple[int, int] | None,
) -> OrderSnapshot | None:
    normalized = normalize_symbol(pair_symbol)
    for item in snapshots:
        if normalize_symbol(item.pair_symbol) != normalized:
            continue
        if side is not None and item.side is not None and item.side != side:
            continue
        if abs(item.price - price) > price_tolerance:
            continue
        if abs(item.quantity - quantity) > qty_tolerance:
            continue
        if time_window is not None:
            start_ms, end_ms = time_window
            observed = item.update_time if item.update_time is not None else item.timestamp
            if observed < start_ms or observed > end_ms:
                continue
        return item
    return None


def quantize_price(price: Decimal, rules: SymbolRules) -> Decimal:
    if price < 0:
        raise ValidationError(f"price must be >= 0 for {rules.pair_symbol}")
    if rules.tick_size is not None and rules.tick_size > 0:
        return (price / rules.tick_size).to_integral_value(rounding=ROUND_DOWN) * rules.tick_size
    quantum = Decimal("1").scaleb(-rules.price_scale)
    return price.quantize(quantum, rounding=ROUND_DOWN)


def quantize_quantity(qty: Decimal, rules: SymbolRules) -> Decimal:
    if qty < 0:
        raise ValidationError(f"quantity must be >= 0 for {rules.pair_symbol}")
    if rules.step_size is not None and rules.step_size > 0:
        return (qty / rules.step_size).to_integral_value(rounding=ROUND_DOWN) * rules.step_size
    quantum = Decimal("1").scaleb(-rules.quantity_scale)
    return qty.quantize(quantum, rounding=ROUND_DOWN)


def validate_order(price: Decimal, qty: Decimal, rules: SymbolRules) -> None:
    if price <= 0:
        raise ValidationError(f"price must be > 0 for {rules.pair_symbol}")
    if qty <= 0:
        raise ValidationError(f"quantity must be > 0 for {rules.pair_symbol}")

    if quantize_price(price, rules) != price:
        raise ValidationError(f"price scale violation for {rules.pair_symbol}")
    if quantize_quantity(qty, rules) != qty:
        raise ValidationError(f"quantity scale violation for {rules.pair_symbol}")

    if rules.min_price is not None and price < rules.min_price:
        raise ValidationError(f"price below min_price for {rules.pair_symbol}")
    if rules.max_price is not None and price > rules.max_price:
        raise ValidationError(f"price above max_price for {rules.pair_symbol}")
    if rules.min_qty is not None and qty < rules.min_qty:
        raise ValidationError(f"quantity below min_qty for {rules.pair_symbol}")
    if rules.max_qty is not None and qty > rules.max_qty:
        raise ValidationError(f"quantity above max_qty for {rules.pair_symbol}")

    total = price * qty
    if rules.min_total is not None and total < rules.min_total:
        raise ValidationError(f"total below min_total for {rules.pair_symbol}")


def pair_info_to_symbol_rules(pair: PairInfo) -> SymbolRules:
    return SymbolRules(
        pair_symbol=pair.pair_symbol,
        price_scale=pair.denominator_scale,
        quantity_scale=pair.numerator_scale,
        min_total=pair.min_total_amount,
        min_price=pair.min_price,
        max_price=pair.max_price,
        min_qty=pair.min_quantity,
        max_qty=pair.max_quantity,
        tick_size=pair.tick_size,
        step_size=pair.step_size,
    )
