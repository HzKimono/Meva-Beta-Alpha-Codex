from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.intent import Intent, to_order_intent
from btcbot.domain.models import (
    ExchangeError,
    ExchangeOrderStatus,
    Order,
    OrderIntent,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    ReconcileOutcome,
    ReconcileStatus,
    fallback_match_by_fields,
    make_client_order_id,
    match_order_by_client_id,
    normalize_symbol,
    quantize_price,
    quantize_quantity,
    validate_order,
)
from btcbot.observability import get_instrumentation
from btcbot.services.market_data_service import MarketDataService
from btcbot.services.state_store import StateStore
from btcbot.services.trading_policy import (
    policy_block_message,
    validate_live_side_effects_policy,
)

logger = logging.getLogger(__name__)

PLACE_ORDER_IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 60 * 60
CANCEL_ORDER_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


class LiveTradingNotArmedError(RuntimeError):
    """Raised when a live side-effect is attempted without explicit arming."""


class ExecutionService:
    def __init__(
        self,
        exchange: ExchangeClient,
        state_store: StateStore,
        market_data_service: MarketDataService | None = None,
        dry_run: bool = True,
        ttl_seconds: int = 120,
        kill_switch: bool = True,
        live_trading_enabled: bool = False,
        safe_mode: bool = False,
        unknown_reprobe_initial_seconds: int = 30,
        unknown_reprobe_max_seconds: int = 900,
        unknown_reprobe_escalation_attempts: int = 8,
        unknown_reprobe_force_observe_only: bool = False,
        unknown_reprobe_force_kill_switch: bool = False,
        unknown_reprobe_max_lookback_seconds: int = 24 * 60 * 60,
    ) -> None:
        self.exchange = exchange
        self.state_store = state_store
        self.market_data_service = market_data_service
        self.dry_run = dry_run
        self.ttl_seconds = ttl_seconds
        self.kill_switch = kill_switch
        self.live_trading_enabled = live_trading_enabled
        self.safe_mode = safe_mode
        self.unknown_reprobe_initial_seconds = max(1, unknown_reprobe_initial_seconds)
        self.unknown_reprobe_max_seconds = max(
            self.unknown_reprobe_initial_seconds,
            unknown_reprobe_max_seconds,
        )
        self.unknown_reprobe_escalation_attempts = max(
            1, unknown_reprobe_escalation_attempts
        )
        self.unknown_reprobe_force_observe_only = unknown_reprobe_force_observe_only
        self.unknown_reprobe_force_kill_switch = unknown_reprobe_force_kill_switch
        self.unknown_reprobe_max_lookback_seconds = max(
            60 * 60, unknown_reprobe_max_lookback_seconds
        )

    def refresh_order_lifecycle(self, symbols: list[str]) -> None:
        normalized_symbols = sorted({normalize_symbol(symbol) for symbol in symbols})
        if not normalized_symbols:
            return

        local_orders = self.state_store.find_open_or_unknown_orders(normalized_symbols)
        orders_by_symbol: dict[str, list] = {}
        for local in local_orders:
            orders_by_symbol.setdefault(local.symbol, []).append(local)

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        for symbol in normalized_symbols:
            recent: list[OrderSnapshot] | None = None
            due_unknown_orders = [
                local
                for local in orders_by_symbol.get(symbol, [])
                if local.status == OrderStatus.UNKNOWN
                and self._is_unknown_probe_due(local.unknown_next_probe_at, now_ms)
            ]
            try:
                open_orders = self.exchange.get_open_orders(symbol)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Lifecycle refresh failed to load open orders",
                    extra={"extra": {"symbol": symbol}},
                )
                continue

            open_snapshots = self._open_items_to_snapshots(
                [*open_orders.bids, *open_orders.asks]
            )
            open_ids = {snapshot.order_id for snapshot in open_snapshots}

            for snapshot in open_snapshots:
                mapped = self._map_exchange_status(snapshot.status)
                self.state_store.update_order_status(
                    order_id=snapshot.order_id,
                    status=mapped,
                    exchange_status_raw=snapshot.status_raw,
                    reconciled=True,
                    last_seen_at=snapshot.update_time or snapshot.timestamp,
                )

            for local in orders_by_symbol.get(symbol, []):
                if local.order_id in open_ids:
                    continue
                if (
                    local.status == OrderStatus.UNKNOWN
                    and not self._is_unknown_probe_due(
                        local.unknown_next_probe_at,
                        now_ms,
                    )
                ):
                    continue

                if recent is None:
                    try:
                        recent = self.exchange.get_all_orders(
                            pair_symbol=symbol,
                            start_ms=self._compute_all_orders_start_ms(
                                now_ms=now_ms,
                                due_unknown_orders=due_unknown_orders,
                            ),
                            end_ms=now_ms,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Lifecycle refresh failed to load all orders",
                            extra={"extra": {"symbol": symbol}},
                        )
                        continue

                matched = self._match_existing_order(
                    local.order_id, local.client_order_id, recent
                )
                if matched is None:
                    if local.status == OrderStatus.UNKNOWN:
                        self._mark_unknown_unresolved(local, now_ms)
                    continue

                mapped = self._map_exchange_status(matched.status)
                self.state_store.update_order_status(
                    order_id=local.order_id,
                    status=mapped,
                    exchange_status_raw=matched.status_raw,
                    reconciled=True,
                    last_seen_at=matched.update_time or matched.timestamp,
                )

    def _compute_all_orders_start_ms(
        self, *, now_ms: int, due_unknown_orders: list
    ) -> int:
        default_start = now_ms - 60 * 60 * 1000
        max_lookback_start = now_ms - self.unknown_reprobe_max_lookback_seconds * 1000
        start_ms = default_start

        unknown_first_seen_candidates: list[int] = []
        for order in due_unknown_orders:
            first_seen = order.unknown_first_seen_at
            if first_seen is None:
                continue
            if first_seen <= 0:
                continue
            if first_seen > now_ms:
                continue
            unknown_first_seen_candidates.append(first_seen)

        if unknown_first_seen_candidates:
            buffer_ms = 60 * 1000
            candidate_start = min(unknown_first_seen_candidates) - buffer_ms
            start_ms = min(default_start, candidate_start)

        return max(max_lookback_start, start_ms)

    def _is_unknown_probe_due(self, next_probe_at: int | None, now_ms: int) -> bool:
        return next_probe_at is None or next_probe_at <= now_ms

    def _mark_unknown_unresolved(self, local_order, now_ms: int) -> None:
        raw_attempts = local_order.unknown_probe_attempts
        safe_attempts = raw_attempts if isinstance(raw_attempts, int) else 0
        safe_attempts = max(0, min(safe_attempts, 60))
        next_attempt = safe_attempts + 1
        backoff_seconds = min(
            self.unknown_reprobe_max_seconds,
            self.unknown_reprobe_initial_seconds * (2 ** (next_attempt - 1)),
        )
        next_probe_at = now_ms + backoff_seconds * 1000
        should_escalate = (
            next_attempt >= self.unknown_reprobe_escalation_attempts
            and local_order.unknown_escalated_at is None
        )
        self.state_store.mark_unknown_probe_result(
            order_id=local_order.order_id,
            last_probe_at=now_ms,
            next_probe_at=next_probe_at,
            escalate=should_escalate,
        )

        if not should_escalate:
            return

        get_instrumentation().counter(
            "unknown_order_retry_escalations_total",
            attrs={"symbol": local_order.symbol, "order_id": local_order.order_id},
        )
        logger.error(
            "Unknown order exceeded retry threshold",
            extra={
                "extra": {
                    "order_id": local_order.order_id,
                    "symbol": local_order.symbol,
                    "attempts": next_attempt,
                }
            },
        )
        if self.unknown_reprobe_force_observe_only:
            self.safe_mode = True
            logger.error("Escalation forcing observe-only mode")
        if self.unknown_reprobe_force_kill_switch:
            self.kill_switch = True
            logger.error("Escalation forcing kill switch")

    def cancel_stale_orders(self, cycle_id: str) -> int:
        try:
            open_orders = self.exchange.list_open_orders()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to list open orders")
            return 0

        if self.safe_mode:
            logger.warning("safe_mode_blocks_cancel_write_calls")
            return 0

        if self.kill_switch:
            for order in open_orders:
                logger.info(
                    "Kill switch active; would cancel order",
                    extra={
                        "extra": {"order_id": order.order_id, "symbol": order.symbol}
                    },
                )
            return 0

        canceled = 0
        self.state_store.prune_expired_idempotency_keys()
        now = datetime.now(UTC)
        for order in open_orders:
            created_at = order.created_at
            if created_at is None or created_at.tzinfo is None:
                logger.warning(
                    "Skipping stale check due to missing/naive timestamp",
                    extra={
                        "extra": {"order_id": order.order_id, "symbol": order.symbol}
                    },
                )
                continue

            if order.status == OrderStatus.UNKNOWN:
                logger.info(
                    "Skipping cancel for unknown order",
                    extra={"extra": {"order_id": order.order_id}},
                )
                continue

            age_seconds = (now - created_at).total_seconds()
            if age_seconds <= self.ttl_seconds:
                continue

            if not self.dry_run:
                self._ensure_live_side_effects_allowed()

            payload_hash = self._cancel_hash(order.order_id)
            idempotency_key = f"cancel:{order.order_id}"
            reservation = self.state_store.reserve_idempotency_key(
                "cancel_order",
                idempotency_key,
                payload_hash,
                ttl_seconds=CANCEL_ORDER_IDEMPOTENCY_TTL_SECONDS,
                allow_promote_simulated=not self.dry_run,
            )
            if not reservation.reserved:
                logger.info(
                    "duplicate idempotency key for cancel_order",
                    extra={
                        "extra": {
                            "order_id": order.order_id,
                            "idempotency_key": idempotency_key,
                            "status": reservation.status,
                        }
                    },
                )
                continue
            action_type = "would_cancel_order" if self.dry_run else "cancel_order"
            action_id = self.state_store.record_action(
                cycle_id,
                action_type,
                payload_hash,
                dedupe_key=f"{action_type}:{idempotency_key}",
            )
            if action_id is None:
                finalized_status = self._handle_reservation_without_new_action(
                    action_type=action_type,
                    idempotency_action_type="cancel_order",
                    idempotency_key=idempotency_key,
                    dedupe_key=f"{action_type}:{idempotency_key}",
                )
                logger.info(
                    "Skipping duplicate cancel action",
                    extra={
                        "extra": {
                            "order_id": order.order_id,
                            "cycle_id": cycle_id,
                            "action_type": action_type,
                            "idempotency_status": finalized_status,
                        }
                    },
                )
                continue
            self.state_store.finalize_idempotency_key(
                "cancel_order",
                idempotency_key,
                action_id=action_id,
                client_order_id=order.client_order_id,
                order_id=order.order_id,
                status="PENDING",
            )

            if self.dry_run:
                logger.info(
                    "Dry-run mode; would cancel stale order",
                    extra={
                        "extra": {"order_id": order.order_id, "symbol": order.symbol}
                    },
                )
                canceled += 1
                self.state_store.finalize_idempotency_key(
                    "cancel_order",
                    idempotency_key,
                    action_id=action_id,
                    client_order_id=order.client_order_id,
                    order_id=order.order_id,
                    status="SIMULATED",
                )
                continue

            try:
                started = datetime.now(UTC)
                was_canceled = self.exchange.cancel_order(order.order_id)
                get_instrumentation().histogram(
                    "cancel_latency_ms",
                    (datetime.now(UTC) - started).total_seconds() * 1000,
                    attrs={"symbol": order.symbol},
                )
            except Exception as exc:  # noqa: BLE001
                if not self._is_uncertain_error(exc):
                    logger.exception(
                        "Failed to cancel stale order",
                        extra={"extra": {"order_id": order.order_id}},
                    )
                    self.state_store.finalize_idempotency_key(
                        "cancel_order",
                        idempotency_key,
                        action_id=action_id,
                        client_order_id=order.client_order_id,
                        order_id=order.order_id,
                        status="FAILED",
                    )
                    self.state_store.clear_action_dedupe_key(action_id)
                    continue

                outcome = self._reconcile_cancel(order)
                if outcome.status == ReconcileStatus.CONFIRMED:
                    canceled += 1

                self.state_store.attach_action_metadata(
                    action_id=action_id,
                    client_order_id=order.client_order_id,
                    order_id=order.order_id,
                    reconciled=outcome.status == ReconcileStatus.CONFIRMED,
                    reconcile_status=outcome.status.value,
                    reconcile_reason=outcome.reason,
                )
                if outcome.status == ReconcileStatus.UNKNOWN:
                    self.state_store.update_order_status(
                        order_id=order.order_id,
                        status=OrderStatus.UNKNOWN,
                        reconciled=True,
                    )
                    self.state_store.finalize_idempotency_key(
                        "cancel_order",
                        idempotency_key,
                        action_id=action_id,
                        client_order_id=order.client_order_id,
                        order_id=order.order_id,
                        status="UNKNOWN",
                    )
                elif outcome.status == ReconcileStatus.CONFIRMED:
                    resolved = (
                        OrderStatus.FILLED
                        if "filled" in outcome.reason.lower()
                        else OrderStatus.CANCELED
                    )
                    self.state_store.update_order_status(
                        order_id=order.order_id, status=resolved, reconciled=True
                    )
                    self.state_store.finalize_idempotency_key(
                        "cancel_order",
                        idempotency_key,
                        action_id=action_id,
                        client_order_id=order.client_order_id,
                        order_id=order.order_id,
                        status="COMMITTED",
                    )
                else:
                    self.state_store.finalize_idempotency_key(
                        "cancel_order",
                        idempotency_key,
                        action_id=action_id,
                        client_order_id=order.client_order_id,
                        order_id=order.order_id,
                        status="FAILED",
                    )
                    self.state_store.clear_action_dedupe_key(action_id)
                continue

            if not was_canceled:
                self.state_store.finalize_idempotency_key(
                    "cancel_order",
                    idempotency_key,
                    action_id=action_id,
                    client_order_id=order.client_order_id,
                    order_id=order.order_id,
                    status="FAILED",
                )
                self.state_store.clear_action_dedupe_key(action_id)
                continue

            canceled += 1
            self.state_store.update_order_status(
                order_id=order.order_id, status=OrderStatus.CANCELED
            )
            self.state_store.attach_action_metadata(
                action_id=action_id,
                client_order_id=order.client_order_id,
                order_id=order.order_id,
                reconciled=False,
                reconcile_status=None,
                reconcile_reason=None,
            )
            self.state_store.finalize_idempotency_key(
                "cancel_order",
                idempotency_key,
                action_id=action_id,
                client_order_id=order.client_order_id,
                order_id=order.order_id,
                status="COMMITTED",
            )
        return canceled

    def execute_intents(
        self, intents: list[OrderIntent] | list[Intent], *, cycle_id: str | None = None
    ) -> int:
        normalized_intents: list[tuple[OrderIntent, Intent | None]] = []
        for raw in intents:
            if isinstance(raw, Intent):
                if cycle_id is None:
                    raise ValueError(
                        "cycle_id is required when executing Stage 3 Intent inputs"
                    )
                normalized_intents.append(
                    (to_order_intent(raw, cycle_id=cycle_id), raw)
                )
            else:
                normalized_intents.append((raw, None))

        symbols = [intent.symbol for intent, _ in normalized_intents]
        self.refresh_order_lifecycle(symbols)
        self.state_store.prune_expired_idempotency_keys()

        if self.safe_mode:
            logger.warning("safe_mode_blocks_submit_write_calls")
            return 0

        if self.kill_switch:
            for intent, _raw_intent in normalized_intents:
                logger.info(
                    "Kill switch active; would place order",
                    extra={
                        "extra": {
                            "symbol": intent.symbol,
                            "side": intent.side.value,
                            "price": intent.price,
                            "quantity": intent.quantity,
                        }
                    },
                )
            return 0

        placed = 0
        for intent, raw_intent in normalized_intents:
            if not self.dry_run:
                self._ensure_live_side_effects_allowed()

            payload_hash = self._place_hash(intent)
            idempotency_key = self._place_idempotency_key(intent, raw_intent)
            reservation = self.state_store.reserve_idempotency_key(
                "place_order",
                idempotency_key,
                payload_hash,
                ttl_seconds=PLACE_ORDER_IDEMPOTENCY_TTL_SECONDS,
                allow_promote_simulated=not self.dry_run,
            )
            if not reservation.reserved:
                logger.info(
                    "duplicate idempotency key for place_order",
                    extra={
                        "extra": {
                            "idempotency_key": idempotency_key,
                            "status": reservation.status,
                            "action_id": reservation.action_id,
                        }
                    },
                )
                continue
            action_type = "would_place_order" if self.dry_run else "place_order"
            action_id = self.state_store.record_action(
                intent.cycle_id,
                action_type,
                payload_hash,
                dedupe_key=f"{action_type}:{idempotency_key}",
            )
            if action_id is None:
                finalized_status = self._handle_reservation_without_new_action(
                    action_type=action_type,
                    idempotency_action_type="place_order",
                    idempotency_key=idempotency_key,
                    dedupe_key=f"{action_type}:{idempotency_key}",
                )
                logger.info(
                    "Skipping duplicate place action",
                    extra={
                        "extra": {
                            "symbol": intent.symbol,
                            "cycle_id": intent.cycle_id,
                            "action_type": action_type,
                            "idempotency_status": finalized_status,
                        }
                    },
                )
                continue
            self.state_store.finalize_idempotency_key(
                "place_order",
                idempotency_key,
                action_id=action_id,
                client_order_id=None,
                order_id=None,
                status="PENDING",
            )

            client_order_id = make_client_order_id(intent)
            if self.dry_run:
                self.state_store.attach_action_metadata(
                    action_id=action_id,
                    client_order_id=client_order_id,
                    order_id=None,
                    reconciled=False,
                    reconcile_status=None,
                    reconcile_reason=None,
                    idempotency_key=(
                        idempotency_key
                    ),
                    intent_id=(raw_intent.intent_id if raw_intent else None),
                )
                self.state_store.finalize_idempotency_key(
                    "place_order",
                    idempotency_key,
                    action_id=action_id,
                    client_order_id=client_order_id,
                    order_id=None,
                    status="SIMULATED",
                )
                placed += 1
                continue

            if self.market_data_service is None:
                raise LiveTradingNotArmedError(
                    "MarketDataService is required for live order validation"
                )

            symbol_normalized = normalize_symbol(intent.symbol)
            rules = self.market_data_service.get_symbol_rules(symbol_normalized)
            try:
                price = quantize_price(Decimal(str(intent.price)), rules)
                quantity = quantize_quantity(Decimal(str(intent.quantity)), rules)
                validate_order(price=price, qty=quantity, rules=rules)
            except ValueError:
                logger.exception("Intent failed symbol rule validation")
                self.state_store.finalize_idempotency_key(
                    "place_order",
                    idempotency_key,
                    action_id=action_id,
                    client_order_id=client_order_id,
                    order_id=None,
                    status="FAILED",
                )
                self.state_store.clear_action_dedupe_key(action_id)
                continue

            try:
                order = self.exchange.place_limit_order(
                    symbol=symbol_normalized,
                    side=intent.side,
                    price=float(price),
                    quantity=float(quantity),
                    client_order_id=client_order_id,
                )
            except Exception as exc:  # noqa: BLE001
                if not self._is_uncertain_error(exc):
                    if isinstance(exc, ExchangeError):
                        logger.error(
                            "Failed to place limit order",
                            extra={
                                "extra": {
                                    "error_type": type(exc).__name__,
                                    "status_code": exc.status_code,
                                    "error_code": exc.error_code,
                                    "safe_message": exc.error_message
                                    or "exchange submit failed",
                                    "request_method": exc.request_method,
                                    "request_path": exc.request_path,
                                    "request_params": exc.request_params,
                                    "request_json": exc.request_json,
                                    "response_body": exc.response_body,
                                }
                            },
                        )
                    else:
                        logger.exception("Failed to place limit order")
                    self.state_store.finalize_idempotency_key(
                        "place_order",
                        idempotency_key,
                        action_id=action_id,
                        client_order_id=client_order_id,
                        order_id=None,
                        status="FAILED",
                    )
                    self.state_store.clear_action_dedupe_key(action_id)
                    continue

                outcome = self._reconcile_submit(
                    symbol_normalized=symbol_normalized,
                    side=intent.side,
                    price=price,
                    quantity=quantity,
                    client_order_id=client_order_id,
                )
                if (
                    outcome.status != ReconcileStatus.CONFIRMED
                    or outcome.order_id is None
                ):
                    self.state_store.attach_action_metadata(
                        action_id=action_id,
                        client_order_id=client_order_id,
                        order_id=None,
                        reconciled=False,
                        reconcile_status=outcome.status.value,
                        reconcile_reason=outcome.reason,
                        idempotency_key=(
                            idempotency_key
                        ),
                        intent_id=(raw_intent.intent_id if raw_intent else None),
                    )
                    self.state_store.finalize_idempotency_key(
                        "place_order",
                        idempotency_key,
                        action_id=action_id,
                        client_order_id=client_order_id,
                        order_id=None,
                        status="UNKNOWN",
                    )
                    continue

                order = Order(
                    order_id=outcome.order_id,
                    client_order_id=client_order_id,
                    symbol=symbol_normalized,
                    side=intent.side,
                    price=float(price),
                    quantity=float(quantity),
                    status=OrderStatus.NEW,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                self.state_store.save_order(
                    order,
                    reconciled=True,
                    idempotency_key=(
                        idempotency_key
                    ),
                    intent_id=(raw_intent.intent_id if raw_intent else None),
                )
                self.state_store.update_order_status(
                    order_id=order.order_id,
                    status=OrderStatus.OPEN,
                    reconciled=True,
                )
                self.state_store.attach_action_metadata(
                    action_id=action_id,
                    client_order_id=client_order_id,
                    order_id=order.order_id,
                    reconciled=True,
                    reconcile_status=outcome.status.value,
                    reconcile_reason=outcome.reason,
                    idempotency_key=(
                        idempotency_key
                    ),
                    intent_id=(raw_intent.intent_id if raw_intent else None),
                )
                self.state_store.finalize_idempotency_key(
                    "place_order",
                    idempotency_key,
                    action_id=action_id,
                    client_order_id=client_order_id,
                    order_id=order.order_id,
                    status="COMMITTED",
                )
                placed += 1
                continue

            self.state_store.save_order(
                order,
                idempotency_key=(
                    idempotency_key
                ),
                intent_id=(raw_intent.intent_id if raw_intent else None),
            )
            self.state_store.attach_action_metadata(
                action_id=action_id,
                client_order_id=client_order_id,
                order_id=order.order_id,
                reconciled=False,
                reconcile_status=None,
                reconcile_reason=None,
                idempotency_key=(
                    idempotency_key
                ),
                intent_id=(raw_intent.intent_id if raw_intent else None),
            )
            self.state_store.finalize_idempotency_key(
                "place_order",
                idempotency_key,
                action_id=action_id,
                client_order_id=client_order_id,
                order_id=order.order_id,
                status="COMMITTED",
            )
            placed += 1
        return placed

    def _place_idempotency_key(
        self,
        intent: OrderIntent,
        raw_intent: Intent | None,
    ) -> str:
        if raw_intent and raw_intent.idempotency_key:
            return raw_intent.idempotency_key
        return self._stable_place_intent_hash(intent)

    def _stable_place_intent_hash(self, intent: OrderIntent) -> str:
        digest = hashlib.sha256()
        digest.update(
            "|".join(
                [
                    normalize_symbol(intent.symbol),
                    intent.side.value,
                    format(Decimal(str(intent.price)), "f"),
                    format(Decimal(str(intent.quantity)), "f"),
                    str(intent.cycle_id),
                ]
            ).encode("utf-8")
        )
        return f"place:{digest.hexdigest()}"

    def _handle_reservation_without_new_action(
        self,
        *,
        action_type: str,
        idempotency_action_type: str,
        idempotency_key: str,
        dedupe_key: str,
    ) -> str:
        existing_action = self.state_store.get_action_by_dedupe_key(dedupe_key)
        if existing_action is not None:
            status = "SIMULATED" if action_type.startswith("would_") else "COMMITTED"
            self.state_store.finalize_idempotency_key(
                idempotency_action_type,
                idempotency_key,
                action_id=int(existing_action["id"]),
                client_order_id=(
                    str(existing_action["client_order_id"])
                    if existing_action["client_order_id"] is not None
                    else None
                ),
                order_id=(
                    str(existing_action["order_id"])
                    if existing_action["order_id"] is not None
                    else None
                ),
                status=status,
            )
            return status
        logger.error(
            "idempotency reservation had no matching action dedupe row",
            extra={
                "extra": {
                    "action_type": action_type,
                    "idempotency_action_type": idempotency_action_type,
                    "idempotency_key": idempotency_key,
                    "dedupe_key": dedupe_key,
                }
            },
        )
        self.state_store.finalize_idempotency_key(
            idempotency_action_type,
            idempotency_key,
            action_id=None,
            client_order_id=None,
            order_id=None,
            status="FAILED",
        )
        return "FAILED"

    def _reconcile_submit(
        self,
        *,
        symbol_normalized: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str,
    ) -> ReconcileOutcome:
        try:
            open_orders = self.exchange.get_open_orders(symbol_normalized)
            snapshots = self._open_items_to_snapshots(
                [*open_orders.bids, *open_orders.asks]
            )
            matched = match_order_by_client_id(snapshots, client_order_id)
            if matched is not None:
                return ReconcileOutcome(
                    status=ReconcileStatus.CONFIRMED,
                    order_id=matched.order_id,
                    reason="found in openOrders by client_order_id",
                )

            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            window = (now_ms - 5 * 60 * 1000, now_ms)
            matched = fallback_match_by_fields(
                snapshots,
                pair_symbol=symbol_normalized,
                side=side,
                price=price,
                quantity=quantity,
                price_tolerance=Decimal("0.00000001"),
                qty_tolerance=Decimal("0.00000001"),
                time_window=window,
            )
            if matched is not None:
                return ReconcileOutcome(
                    status=ReconcileStatus.CONFIRMED,
                    order_id=matched.order_id,
                    reason="found in openOrders by fallback fields",
                )

            all_orders = self.exchange.get_all_orders(
                pair_symbol=symbol_normalized,
                start_ms=window[0],
                end_ms=window[1],
            )
            matched = match_order_by_client_id(all_orders, client_order_id)
            if matched is not None:
                return ReconcileOutcome(
                    status=ReconcileStatus.CONFIRMED,
                    order_id=matched.order_id,
                    reason="found in allOrders by client_order_id",
                )

            matched = fallback_match_by_fields(
                all_orders,
                pair_symbol=symbol_normalized,
                side=side,
                price=price,
                quantity=quantity,
                price_tolerance=Decimal("0.00000001"),
                qty_tolerance=Decimal("0.00000001"),
                time_window=window,
            )
            if matched is not None:
                return ReconcileOutcome(
                    status=ReconcileStatus.CONFIRMED,
                    order_id=matched.order_id,
                    reason="found in allOrders by fallback fields",
                )
        except Exception:  # noqa: BLE001
            logger.exception("Submit reconciliation failed")
            return ReconcileOutcome(
                status=ReconcileStatus.UNKNOWN, order_id=None, reason="reconcile error"
            )

        return ReconcileOutcome(
            status=ReconcileStatus.NOT_FOUND,
            order_id=None,
            reason="not found after checks",
        )

    def _reconcile_cancel(self, order: Order) -> ReconcileOutcome:
        symbol = normalize_symbol(order.symbol)
        try:
            open_orders = self.exchange.get_open_orders(symbol)
            for item in [*open_orders.bids, *open_orders.asks]:
                if str(item.id) == order.order_id:
                    return ReconcileOutcome(
                        status=ReconcileStatus.UNKNOWN,
                        order_id=order.order_id,
                        reason="order still present in openOrders",
                    )

            recent = self.exchange.get_all_orders(
                pair_symbol=symbol,
                start_ms=int((datetime.now(UTC).timestamp() - 3600) * 1000),
                end_ms=int(datetime.now(UTC).timestamp() * 1000),
            )
            matched = self._match_existing_order(
                order.order_id, order.client_order_id, recent
            )
            if matched is None:
                return ReconcileOutcome(
                    status=ReconcileStatus.UNKNOWN,
                    order_id=order.order_id,
                    reason="order absent in openOrders but not found in allOrders",
                )
            if matched.status == ExchangeOrderStatus.FILLED:
                return ReconcileOutcome(
                    status=ReconcileStatus.CONFIRMED,
                    order_id=order.order_id,
                    reason="order filled",
                )
            if matched.status == ExchangeOrderStatus.CANCELED:
                return ReconcileOutcome(
                    status=ReconcileStatus.CONFIRMED,
                    order_id=order.order_id,
                    reason="order canceled",
                )
            return ReconcileOutcome(
                status=ReconcileStatus.UNKNOWN,
                order_id=order.order_id,
                reason="order status unresolved",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Cancel reconciliation failed")
            return ReconcileOutcome(
                status=ReconcileStatus.UNKNOWN,
                order_id=order.order_id,
                reason="reconcile error",
            )

    def _match_existing_order(
        self,
        order_id: str,
        client_order_id: str | None,
        snapshots: list[OrderSnapshot],
    ) -> OrderSnapshot | None:
        for snapshot in snapshots:
            if snapshot.order_id == order_id:
                return snapshot
        if client_order_id is None:
            return None
        return match_order_by_client_id(snapshots, client_order_id)

    def _open_items_to_snapshots(self, items: list[object]) -> list[OrderSnapshot]:
        snapshots: list[OrderSnapshot] = []
        for item in items:
            method = str(getattr(item, "method", "")).strip().lower()
            side = None
            if method == "buy":
                side = OrderSide.BUY
            elif method == "sell":
                side = OrderSide.SELL

            snapshots.append(
                OrderSnapshot(
                    order_id=str(item.id),
                    client_order_id=getattr(item, "order_client_id", None),
                    pair_symbol=str(item.pair_symbol),
                    side=side,
                    price=Decimal(str(item.price)),
                    quantity=Decimal(str(item.quantity)),
                    status=ExchangeOrderStatus.OPEN,
                    timestamp=int(item.time),
                    update_time=(
                        int(item.update_time)
                        if getattr(item, "update_time", None) is not None
                        else None
                    ),
                    status_raw=str(getattr(item, "status", "open")),
                )
            )
        return snapshots

    def _map_exchange_status(self, status: ExchangeOrderStatus) -> OrderStatus:
        return {
            ExchangeOrderStatus.OPEN: OrderStatus.OPEN,
            ExchangeOrderStatus.PARTIAL: OrderStatus.PARTIAL,
            ExchangeOrderStatus.FILLED: OrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED: OrderStatus.CANCELED,
            ExchangeOrderStatus.REJECTED: OrderStatus.REJECTED,
        }.get(status, OrderStatus.UNKNOWN)

    def _is_uncertain_error(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.TimeoutException):
            return True
        if isinstance(exc, ExchangeError):
            message = str(exc)
            if "status=429" in message:
                return True
            if "status=" in message:
                try:
                    status = int(message.split("status=")[1].split()[0])
                    if status >= 500:
                        return True
                except ValueError:
                    pass
            return "json" in message.lower()
        return "json" in str(exc).lower()

    def _ensure_live_side_effects_allowed(self) -> None:
        reason = validate_live_side_effects_policy(
            dry_run=self.dry_run,
            kill_switch=self.kill_switch,
            live_trading_enabled=self.live_trading_enabled,
        )
        if reason is not None:
            raise LiveTradingNotArmedError(policy_block_message(reason))

    def _place_hash(self, intent: OrderIntent) -> str:
        raw = (
            f"{intent.symbol}|{intent.side.value}|{intent.price:.8f}|"
            f"{intent.quantity:.8f}|{intent.cycle_id}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cancel_hash(self, order_id: str) -> str:
        return hashlib.sha256(order_id.encode()).hexdigest()
