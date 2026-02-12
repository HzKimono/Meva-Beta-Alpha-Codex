from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.allocation import SizedAction
from btcbot.domain.stage4 import Mode, Order
from btcbot.domain.symbols import canonical_symbol


def build_deterministic_client_order_id(action: SizedAction) -> str:
    symbol = canonical_symbol(action.symbol)
    raw = f"s:{action.strategy_id}|i:{action.intent_index}|sym:{symbol}|side:{action.side}"
    sanitized = "".join(ch for ch in raw if ch.isalnum() or ch in {":", "|", "-", "_"})
    return sanitized[:64]


def sized_action_to_order(
    action: SizedAction,
    *,
    mode: Mode,
    created_at: datetime | None = None,
) -> Order:
    ts = created_at or datetime.now(UTC)
    if action.qty <= Decimal("0"):
        raise ValueError("sized action qty must be > 0")
    price = action.notional_try / action.qty
    return Order(
        symbol=canonical_symbol(action.symbol),
        side=action.side,
        type="limit",
        price=price,
        qty=action.qty,
        status="new",
        created_at=ts,
        updated_at=ts,
        client_order_id=build_deterministic_client_order_id(action),
        mode=mode,
    )
