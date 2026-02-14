from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from btcbot.domain.ledger import ensure_utc
from btcbot.domain.risk_models import stable_hash_payload


class OrderStatus(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def make_order_id(client_order_id: str) -> str:
    return f"s7o:{short_hash(client_order_id)}"


def make_event_id(client_order_id: str, seq: int, event_type: str) -> str:
    return f"s7e:{short_hash(f'{client_order_id}:{seq}:{event_type}')}"


@dataclass(frozen=True)
class Stage7Order:
    order_id: str
    client_order_id: str
    cycle_id: str
    symbol: str
    side: str
    order_type: str
    price_try: Decimal
    qty: Decimal
    filled_qty: Decimal
    avg_fill_price_try: Decimal | None
    status: OrderStatus
    last_update: datetime
    intent_hash: str


@dataclass(frozen=True)
class OrderEvent:
    event_id: str
    ts: datetime
    client_order_id: str
    order_id: str
    event_type: str
    payload: dict[str, object]
    cycle_id: str

    def payload_json(self) -> str:
        return json.dumps(
            self.payload, sort_keys=True, separators=(",", ":"), default=_json_default
        )


def make_intent_hash(intent_payload: dict[str, object]) -> str:
    return stable_hash_payload(intent_payload)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return ensure_utc(value).astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported payload type: {type(value).__name__}")
