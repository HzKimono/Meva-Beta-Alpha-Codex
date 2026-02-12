from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from btcbot.domain.models import OrderIntent, OrderSide, normalize_symbol


@dataclass(frozen=True)
class Intent:
    """Strategy/risk/execution intent with stable idempotency identity."""

    intent_id: str
    symbol: str
    side: OrderSide
    qty: Decimal
    limit_price: Decimal | None
    reason: str
    confidence: float
    ttl_seconds: int | None
    idempotency_key: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        *,
        cycle_id: str,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        limit_price: Decimal | None,
        reason: str,
        confidence: float = 0.5,
        ttl_seconds: int | None = None,
        intent_id: str | None = None,
    ) -> Intent:
        normalized_symbol = normalize_symbol(symbol)
        stable_key = build_idempotency_key(
            cycle_id=cycle_id,
            symbol=normalized_symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
        )
        return cls(
            intent_id=intent_id or uuid4().hex,
            symbol=normalized_symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
            reason=reason,
            confidence=max(0.0, min(1.0, confidence)),
            ttl_seconds=ttl_seconds,
            idempotency_key=stable_key,
        )


def build_idempotency_key(
    *, cycle_id: str, symbol: str, side: OrderSide, qty: Decimal, limit_price: Decimal | None
) -> str:
    raw = "|".join(
        [
            cycle_id,
            normalize_symbol(symbol),
            side.value,
            str(qty),
            "" if limit_price is None else str(limit_price),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def to_order_intent(intent: Intent, *, cycle_id: str) -> OrderIntent:
    """Backward-compatible adapter for Stage 2 execution path."""

    if intent.limit_price is None:
        raise ValueError("Intent.limit_price is required for Stage 2 limit execution")
    notional = intent.qty * intent.limit_price
    return OrderIntent(
        symbol=intent.symbol,
        side=intent.side,
        price=float(intent.limit_price),
        quantity=float(intent.qty),
        notional=float(notional),
        cycle_id=cycle_id,
    )
