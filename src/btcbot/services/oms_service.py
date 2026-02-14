from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import normalize_symbol
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.order_state import (
    OrderEvent,
    OrderStatus,
    Stage7Order,
    make_event_id,
    make_intent_hash,
    make_order_id,
)
from btcbot.services.rate_limiter import TokenBucketRateLimiter
from btcbot.services.retry import RetryAttempt, retry_with_backoff
from btcbot.services.state_store import IdempotencyConflictError, StateStore

_TERMINAL_STATUSES = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}


class TransientOMSAdapterError(RuntimeError):
    pass


class NetworkTimeout(TransientOMSAdapterError):
    pass


class RateLimitError(TransientOMSAdapterError):
    pass


class TemporaryUnavailable(TransientOMSAdapterError):
    pass


class NonRetryableOMSAdapterError(RuntimeError):
    pass


class Stage7MarketSimulator:
    def __init__(
        self,
        mark_prices_try: dict[str, Decimal],
        *,
        transient_failures_by_client_order_id: dict[str, list[str]] | None = None,
    ) -> None:
        self.mark_prices_try = {normalize_symbol(k): v for k, v in mark_prices_try.items()}
        self._transient_failures_by_client_order_id = {
            key: list(value) for key, value in (transient_failures_by_client_order_id or {}).items()
        }

    def should_reject(self, intent: OrderIntent, settings: Settings) -> bool:
        if intent.qty <= 0 or intent.price_try <= 0:
            return True
        reject_bps = settings.stage7_sim_reject_prob_bps
        if reject_bps <= 0:
            return False
        bucket = int(intent.client_order_id[-4:], 16) % 10000 if intent.client_order_id else 0
        return bucket < int(reject_bps)

    def maybe_raise_submit_error(self, client_order_id: str) -> None:
        queue = self._transient_failures_by_client_order_id.get(client_order_id)
        if not queue:
            return
        token = queue.pop(0).strip().upper()
        if token == "RATE_LIMIT":
            raise RateLimitError("simulated rate limit")
        if token == "TIMEOUT":
            raise NetworkTimeout("simulated timeout")
        if token == "UNAVAILABLE":
            raise TemporaryUnavailable("simulated service unavailable")
        if token == "NON_RETRYABLE":
            raise NonRetryableOMSAdapterError("simulated non-retryable failure")

    def fill_slices(self, intent: OrderIntent, settings: Settings) -> list[tuple[Decimal, Decimal]]:
        symbol = normalize_symbol(intent.symbol)
        baseline = self.mark_prices_try.get(symbol, intent.price_try)
        sign = Decimal("1") if intent.side.upper() == "BUY" else Decimal("-1")
        applied = baseline * (
            Decimal("1") + sign * (settings.stage7_slippage_bps / Decimal("10000"))
        )
        partial_candidate = (
            int(intent.client_order_id[-1], 16) % 2 == 0 if intent.client_order_id else False
        )
        if partial_candidate and intent.qty > Decimal("0.0001"):
            first = (intent.qty * Decimal("0.6")).quantize(Decimal("0.00000001"))
            first = max(Decimal("0"), min(first, intent.qty))
            second = intent.qty - first
            if first > 0 and second > 0:
                return [(first, applied), (second, applied)]
        return [(intent.qty, applied)]


class OMSService:
    def __init__(
        self,
        *,
        rate_limiter: TokenBucketRateLimiter | None = None,
        jitter_seed: int = 7,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._rate_limiter = rate_limiter
        self._jitter_seed = jitter_seed
        self._sleep_fn = sleep_fn or (lambda _: None)

    def process_intents(
        self,
        *,
        cycle_id: str,
        now_utc: datetime,
        intents: list[OrderIntent],
        market_sim: Stage7MarketSimulator,
        state_store: StateStore,
        settings: Settings,
        cancel_requests: list[str] | None = None,
    ) -> tuple[list[Stage7Order], list[OrderEvent]]:
        applied_orders: list[Stage7Order] = []
        events_to_append: list[OrderEvent] = []
        cancel_set = set(cancel_requests or [])

        limiter = self._rate_limiter or TokenBucketRateLimiter(
            rate_per_sec=float(settings.stage7_rate_limit_rps),
            burst=settings.stage7_rate_limit_burst,
        )

        for intent in sorted(intents, key=lambda item: item.client_order_id):
            if intent.skipped:
                continue

            existing = state_store.get_stage7_order_by_client_id(intent.client_order_id)
            existing_events = state_store.get_stage7_order_events_by_client_id(
                intent.client_order_id
            )
            existing_event_types = [event.event_type for event in existing_events]
            seq = len(existing_events)

            order = existing or Stage7Order(
                order_id=make_order_id(intent.client_order_id),
                client_order_id=intent.client_order_id,
                cycle_id=cycle_id,
                symbol=normalize_symbol(intent.symbol),
                side=intent.side,
                order_type=intent.order_type,
                price_try=intent.price_try,
                qty=intent.qty,
                filled_qty=Decimal("0"),
                avg_fill_price_try=None,
                status=OrderStatus.PLANNED,
                last_update=now_utc.astimezone(UTC),
                intent_hash=make_intent_hash(intent.to_dict()),
            )

            idempotency_key = f"submit:{intent.client_order_id}"
            payload_hash = make_intent_hash(intent.to_dict())
            try:
                registered = state_store.try_register_idempotency_key(idempotency_key, payload_hash)
            except IdempotencyConflictError:
                _, conflict_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="IDEMPOTENCY_CONFLICT",
                    payload={"key": idempotency_key},
                    existing_event_types=existing_event_types,
                    seq=seq,
                )
                if conflict_event is not None:
                    events_to_append.append(conflict_event)
                raise

            if order.status in _TERMINAL_STATUSES:
                _, duplicate_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="DUPLICATE_IGNORED",
                    payload={"key": idempotency_key, "reason": "terminal_order"},
                    existing_event_types=[],
                    seq=seq,
                )
                if duplicate_event is not None:
                    events_to_append.append(duplicate_event)
                applied_orders.append(order)
                continue

            if not registered:
                _, duplicate_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="DUPLICATE_IGNORED",
                    payload={"key": idempotency_key},
                    existing_event_types=[],
                    seq=seq,
                )
                if duplicate_event is not None:
                    events_to_append.append(duplicate_event)
                applied_orders.append(order)
                continue

            if intent.client_order_id in cancel_set:
                order, new_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="CANCELED",
                    payload={"reason": "cancel_requested"},
                    existing_event_types=existing_event_types,
                    seq=seq,
                )
                if new_event is not None:
                    events_to_append.append(new_event)
                applied_orders.append(order)
                continue

            if not limiter.consume():
                order, throttled_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="THROTTLED",
                    payload={
                        "next_eligible_ts": (
                            now_utc.astimezone(UTC)
                            + timedelta(seconds=limiter.seconds_until_available())
                        ).isoformat()
                    },
                    existing_event_types=[],
                    seq=seq,
                )
                if throttled_event is not None:
                    events_to_append.append(throttled_event)
                applied_orders.append(order)
                continue

            submit_cid = intent.client_order_id

            def _submit_adapter(client_order_id: str = submit_cid) -> None:
                market_sim.maybe_raise_submit_error(client_order_id)

            retry_attempts: list[RetryAttempt] = []

            def _on_retry(
                attempt: RetryAttempt,
                *,
                attempts: list[RetryAttempt] = retry_attempts,
            ) -> None:
                attempts.append(attempt)

            try:
                retry_with_backoff(
                    _submit_adapter,
                    max_attempts=settings.stage7_retry_max_attempts,
                    base_delay_ms=settings.stage7_retry_base_delay_ms,
                    max_delay_ms=settings.stage7_retry_max_delay_ms,
                    jitter_seed=self._jitter_seed,
                    retry_on_exceptions=(NetworkTimeout, RateLimitError, TemporaryUnavailable),
                    sleep_fn=self._sleep_fn,
                    on_retry=_on_retry,
                )
                for retry_attempt in retry_attempts:
                    seq += 1
                    events_to_append.append(
                        OrderEvent(
                            event_id=make_event_id(order.client_order_id, seq, "RETRY_SCHEDULED"),
                            ts=now_utc.astimezone(UTC),
                            client_order_id=order.client_order_id,
                            order_id=order.order_id,
                            event_type="RETRY_SCHEDULED",
                            payload={
                                "attempt": retry_attempt.attempt,
                                "delay_ms": retry_attempt.delay_ms,
                                "error_type": retry_attempt.error_type,
                            },
                            cycle_id=order.cycle_id,
                        )
                    )
            except (NetworkTimeout, RateLimitError, TemporaryUnavailable):
                for retry_attempt in retry_attempts:
                    seq += 1
                    events_to_append.append(
                        OrderEvent(
                            event_id=make_event_id(order.client_order_id, seq, "RETRY_SCHEDULED"),
                            ts=now_utc.astimezone(UTC),
                            client_order_id=order.client_order_id,
                            order_id=order.order_id,
                            event_type="RETRY_SCHEDULED",
                            payload={
                                "attempt": retry_attempt.attempt,
                                "delay_ms": retry_attempt.delay_ms,
                                "error_type": retry_attempt.error_type,
                            },
                            cycle_id=order.cycle_id,
                        )
                    )
                order, giveup_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="RETRY_GIVEUP",
                    payload={"reason": "transient_failure_exhausted"},
                    existing_event_types=[],
                    seq=seq,
                )
                if giveup_event is not None:
                    events_to_append.append(giveup_event)
                applied_orders.append(order)
                continue

            for event_type in ("SUBMIT_REQUESTED", "ACKED"):
                order, new_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type=event_type,
                    payload={"symbol": order.symbol, "side": order.side},
                    existing_event_types=existing_event_types,
                    seq=seq,
                )
                if new_event is not None:
                    events_to_append.append(new_event)
                    existing_event_types.append(event_type)

            if market_sim.should_reject(intent, settings):
                order, rejected_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="REJECTED",
                    payload={"reason": "invalid_price_or_qty"},
                    existing_event_types=existing_event_types,
                    seq=seq,
                )
                if rejected_event is not None:
                    events_to_append.append(rejected_event)
                applied_orders.append(order)
                continue

            slices = market_sim.fill_slices(intent, settings)
            if len(slices) > 1:
                first_qty, first_px = slices[0]
                order, partial_event, seq = self._transition_once(
                    order=order,
                    now_utc=now_utc,
                    event_type="PARTIAL_FILL",
                    payload={"fill_qty": str(first_qty), "fill_price_try": str(first_px)},
                    existing_event_types=existing_event_types,
                    seq=seq,
                    fill_qty=first_qty,
                    fill_price=first_px,
                    target_status=OrderStatus.PARTIALLY_FILLED,
                )
                if partial_event is not None:
                    events_to_append.append(partial_event)
                    existing_event_types.append("PARTIAL_FILL")

            total_fill_qty = sum((qty for qty, _ in slices), Decimal("0"))
            fill_price = slices[-1][1]
            order, filled_event, seq = self._transition_once(
                order=order,
                now_utc=now_utc,
                event_type="FILLED",
                payload={"filled_qty": str(total_fill_qty), "avg_fill_price_try": str(fill_price)},
                existing_event_types=existing_event_types,
                seq=seq,
                fill_qty=total_fill_qty - order.filled_qty,
                fill_price=fill_price,
                target_status=OrderStatus.FILLED,
            )
            if filled_event is not None:
                events_to_append.append(filled_event)

            applied_orders.append(order)

        state_store.upsert_stage7_orders(applied_orders)
        state_store.append_stage7_order_events(events_to_append)
        return applied_orders, events_to_append

    def reconcile_open_orders(
        self,
        *,
        cycle_id: str,
        now_utc: datetime,
        state_store: StateStore,
        settings: Settings,
        market_sim: Stage7MarketSimulator,
    ) -> tuple[list[Stage7Order], list[OrderEvent]]:
        intents: list[OrderIntent] = []
        for order in state_store.load_non_terminal_orders():
            intents.append(
                OrderIntent(
                    cycle_id=cycle_id,
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    price_try=order.price_try,
                    qty=order.qty,
                    notional_try=order.price_try * order.qty,
                    client_order_id=order.client_order_id,
                    reason="reconcile",
                    constraints_applied={},
                )
            )
        return self.process_intents(
            cycle_id=cycle_id,
            now_utc=now_utc,
            intents=intents,
            market_sim=market_sim,
            state_store=state_store,
            settings=settings,
            cancel_requests=[],
        )

    def _transition_once(
        self,
        *,
        order: Stage7Order,
        now_utc: datetime,
        event_type: str,
        payload: dict[str, object],
        existing_event_types: list[str],
        seq: int,
        fill_qty: Decimal = Decimal("0"),
        fill_price: Decimal | None = None,
        target_status: OrderStatus | None = None,
    ) -> tuple[Stage7Order, OrderEvent | None, int]:
        if event_type in existing_event_types:
            return order, None, seq

        status = target_status
        if status is None:
            status = {
                "SUBMIT_REQUESTED": OrderStatus.SUBMITTED,
                "ACKED": OrderStatus.ACKED,
                "REJECTED": OrderStatus.REJECTED,
                "CANCELED": OrderStatus.CANCELED,
            }.get(event_type, order.status)

        allowed_transitions: dict[OrderStatus, set[OrderStatus]] = {
            OrderStatus.PLANNED: {
                OrderStatus.SUBMITTED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            },
            OrderStatus.SUBMITTED: {OrderStatus.ACKED, OrderStatus.CANCELED, OrderStatus.REJECTED},
            OrderStatus.ACKED: {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            },
            OrderStatus.PARTIALLY_FILLED: {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
            },
            OrderStatus.FILLED: set(),
            OrderStatus.CANCELED: set(),
            OrderStatus.REJECTED: set(),
        }
        if status != order.status and status not in allowed_transitions[order.status]:
            return order, None, seq

        new_filled_qty = order.filled_qty + fill_qty
        avg_fill = order.avg_fill_price_try
        if fill_price is not None and fill_qty > 0:
            prior_notional = (order.avg_fill_price_try or Decimal("0")) * order.filled_qty
            new_notional = prior_notional + (fill_price * fill_qty)
            if new_filled_qty > 0:
                avg_fill = new_notional / new_filled_qty

        next_order = replace(
            order,
            status=status,
            filled_qty=new_filled_qty,
            avg_fill_price_try=avg_fill,
            last_update=now_utc.astimezone(UTC),
        )
        next_seq = seq + 1
        event = OrderEvent(
            event_id=make_event_id(order.client_order_id, next_seq, event_type),
            ts=now_utc.astimezone(UTC),
            client_order_id=order.client_order_id,
            order_id=order.order_id,
            event_type=event_type,
            payload=payload,
            cycle_id=order.cycle_id,
        )
        return next_order, event, next_seq
