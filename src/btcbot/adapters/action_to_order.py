from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.allocation import SizedAction
from btcbot.domain.models import PairInfo
from btcbot.domain.stage4 import ExchangeRules, Mode, Order, Quantizer
from btcbot.domain.symbols import canonical_symbol


def build_deterministic_client_order_id(action: SizedAction) -> str:
    symbol = canonical_symbol(action.symbol)
    raw = f"s:{action.strategy_id}|i:{action.intent_index}|sym:{symbol}|side:{action.side}"
    sanitized = "".join(ch for ch in raw if ch.isalnum() or ch in {":", "|", "-", "_"})
    return sanitized[:64]


def build_exchange_rules(pair: PairInfo) -> ExchangeRules:
    price_precision = int(pair.denominator_scale)
    qty_precision = int(pair.numerator_scale)
    return ExchangeRules(
        tick_size=Decimal("1").scaleb(-price_precision),
        step_size=Decimal("1").scaleb(-qty_precision),
        min_notional_try=Decimal(str(pair.min_total_amount or Decimal("0"))),
        price_precision=price_precision,
        qty_precision=qty_precision,
    )


def sized_action_to_order(
    action: SizedAction,
    *,
    mode: Mode,
    mark_price: Decimal | None,
    pair_info: PairInfo | None,
    created_at: datetime | None = None,
) -> tuple[Order | None, str | None]:
    if pair_info is None:
        return None, "dropped_missing_pair_info"
    if mark_price is None or mark_price <= Decimal("0"):
        return None, "missing_mark_price"

    symbol = canonical_symbol(action.symbol)
    rules = build_exchange_rules(pair_info)
    price = Quantizer.quantize_price(mark_price, rules)
    qty_q = Quantizer.quantize_qty(action.qty, rules)
    if qty_q <= Decimal("0"):
        return None, "dropped_qty_became_zero"
    if not Quantizer.validate_min_notional(price, qty_q, rules):
        return None, "dropped_min_notional_after_quantize"

    ts = created_at or datetime.now(UTC)
    return (
        Order(
            symbol=symbol,
            side=action.side,
            type="limit",
            price=price,
            qty=qty_q,
            status="new",
            created_at=ts,
            updated_at=ts,
            client_order_id=build_deterministic_client_order_id(action),
            mode=mode,
        ),
        None,
    )
