from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, Order


@dataclass(frozen=True)
class LifecyclePlan:
    actions: list[LifecycleAction]
    audit_reasons: list[str]


class OrderLifecycleService:
    def __init__(
        self,
        *,
        stale_after_sec: int = 120,
        drift_bps: Decimal = Decimal("25"),
        price_tolerance_bps: Decimal = Decimal("5"),
        qty_tolerance_ratio: Decimal = Decimal("0.01"),
    ) -> None:
        self.stale_after_sec = stale_after_sec
        self.drift_bps = drift_bps
        self.price_tolerance_bps = price_tolerance_bps
        self.qty_tolerance_ratio = qty_tolerance_ratio

    def plan(
        self,
        intents: list[Order],
        open_orders: list[Order],
        *,
        mid_price: Decimal,
    ) -> LifecyclePlan:
        actions: list[LifecycleAction] = []
        audit_reasons: list[str] = []
        stale_by_key: dict[tuple[str, str], list[Order]] = {}

        for existing in open_orders:
            if self._is_stale(existing, mid_price=mid_price):
                stale_by_key.setdefault((existing.symbol, existing.side), []).append(existing)

        for intent in intents:
            if intent.price <= 0 or intent.qty <= 0:
                audit_reasons.append(f"invalid_intent:{intent.client_order_id or 'missing'}")
                continue
            duplicate = self._find_duplicate(intent, open_orders)
            if duplicate is not None:
                audit_reasons.append(f"deduped:{intent.client_order_id or 'missing'}")
                continue

            key = (intent.symbol, intent.side)
            stale_orders = stale_by_key.get(key, [])
            if stale_orders:
                for stale in stale_orders:
                    actions.append(
                        LifecycleAction(
                            action_type=LifecycleActionType.CANCEL,
                            symbol=stale.symbol,
                            side=stale.side,
                            price=stale.price,
                            qty=stale.qty,
                            reason="replace_cancel",
                            client_order_id=stale.client_order_id,
                            exchange_order_id=stale.exchange_order_id,
                        )
                    )
                actions.append(
                    LifecycleAction(
                        action_type=LifecycleActionType.SUBMIT,
                        symbol=intent.symbol,
                        side=intent.side,
                        price=intent.price,
                        qty=intent.qty,
                        reason="replace_submit",
                        client_order_id=intent.client_order_id,
                        replace_for_client_order_id=stale_orders[0].client_order_id,
                    )
                )
                stale_by_key[key] = []
                continue

            actions.append(
                LifecycleAction(
                    action_type=LifecycleActionType.SUBMIT,
                    symbol=intent.symbol,
                    side=intent.side,
                    price=intent.price,
                    qty=intent.qty,
                    reason="new_intent",
                    client_order_id=intent.client_order_id,
                )
            )

        for stale_orders in stale_by_key.values():
            for stale in stale_orders:
                actions.append(
                    LifecycleAction(
                        action_type=LifecycleActionType.CANCEL,
                        symbol=stale.symbol,
                        side=stale.side,
                        price=stale.price,
                        qty=stale.qty,
                        reason="stale",
                        client_order_id=stale.client_order_id,
                        exchange_order_id=stale.exchange_order_id,
                    )
                )

        priority = {
            LifecycleActionType.CANCEL: 0,
            LifecycleActionType.REPLACE: 1,
            LifecycleActionType.SUBMIT: 2,
        }
        ordered = sorted(
            actions,
            key=lambda item: (
                item.symbol,
                item.side,
                priority[item.action_type],
                item.client_order_id or "",
            ),
        )
        return LifecyclePlan(actions=ordered, audit_reasons=audit_reasons)

    def _find_duplicate(self, intent: Order, open_orders: list[Order]) -> Order | None:
        if intent.price <= 0 or intent.qty <= 0:
            return None
        for order in open_orders:
            if order.symbol != intent.symbol or order.side != intent.side:
                continue
            if order.price <= 0 or order.qty <= 0:
                continue
            price_diff_bps = abs(order.price - intent.price) / intent.price * Decimal("10000")
            qty_diff_ratio = abs(order.qty - intent.qty) / intent.qty
            if (
                price_diff_bps <= self.price_tolerance_bps
                and qty_diff_ratio <= self.qty_tolerance_ratio
            ):
                return order
        return None

    def _is_stale(self, order: Order, *, mid_price: Decimal) -> bool:
        now = datetime.now(UTC)
        age = (now - order.updated_at).total_seconds()
        if age >= self.stale_after_sec:
            return True
        if mid_price <= 0 or order.price <= 0:
            return False
        drift = abs(order.price - mid_price) / mid_price * Decimal("10000")
        return drift >= self.drift_bps
