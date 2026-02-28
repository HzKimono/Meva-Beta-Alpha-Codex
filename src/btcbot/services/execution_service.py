from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from btcbot.adapters.exchange import ExchangeClient
from btcbot.domain.decision_codes import ReasonCode
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
    SubmitOrderResult,
    SymbolRules,
    fallback_match_by_fields,
    make_client_order_id,
    map_exchange_ack_to_submit_result,
    match_order_by_client_id,
    normalize_symbol,
    quantize_price,
    quantize_quantity,
)
from btcbot.domain.money_policy import OrderSizingStatus, size_order_from_notional
from btcbot.domain.symbols import split_symbol
from btcbot.obs.metrics import inc_counter
from btcbot.obs.process_role import coerce_process_role, get_process_role_from_env
from btcbot.observability import get_instrumentation
from btcbot.observability_decisions import emit_decision
from btcbot.security.secrets import is_trading_blocked_by_policy
from btcbot.services.execution_errors import ExecutionErrorCategory, classify_exchange_error
from btcbot.services.execution_wrapper import ExecutionWrapper, UncertainResult
from btcbot.services.market_data_service import MarketDataService
from btcbot.services.state_store import PENDING_GRACE_SECONDS, StateStore
from btcbot.services.trading_policy import policy_reason_to_code, validate_live_side_effects_policy
from btcbot.services.unknown_order_registry import UnknownOrderRecord, UnknownOrderRegistry

logger = logging.getLogger(__name__)

PLACE_ORDER_IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 60 * 60
CANCEL_ORDER_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
MAX_CYCLE_BALANCE_SNAPSHOTS = 10
MAX_CYCLE_LIFECYCLE_MARKERS = 10


class SubmitBlockedDueToUnknownError(RuntimeError):
    """Raised when new submit actions are blocked due to unknown order uncertainty."""


class LiveTradingNotArmedError(RuntimeError):
    """Raised when a live side-effect is attempted without explicit arming."""

    def __init__(
        self,
        message: str,
        reasons: list[str] | None = None,
        reason_codes: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons or ())
        self.reason_codes = tuple(reason_codes or ())


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
        live_trading_ack: bool = False,
        safe_mode: bool = False,
        unknown_reprobe_initial_seconds: int = 30,
        unknown_reprobe_max_seconds: int = 900,
        unknown_reprobe_escalation_attempts: int = 8,
        unknown_reprobe_force_observe_only: bool = False,
        unknown_reprobe_force_kill_switch: bool = False,
        process_role: str | None = None,
        unknown_reprobe_max_lookback_seconds: int = 24 * 60 * 60,
        pending_recovery_max_attempts: int = 3,
        pending_recovery_backoff_seconds: int = 30,
        pending_grace_seconds: int = PENDING_GRACE_SECONDS,
        submit_retry_max_attempts: int = 2,
        cancel_retry_max_attempts: int = 2,
        retry_base_delay_ms: int = 250,
        retry_max_delay_ms: int = 4000,
        spot_sell_requires_inventory: bool = True,
        sleep_fn=None,
    ) -> None:
        self.exchange = exchange
        self.state_store = state_store
        self.market_data_service = market_data_service
        self.dry_run = dry_run
        self.ttl_seconds = ttl_seconds
        self.kill_switch = kill_switch
        self.live_trading_enabled = live_trading_enabled
        self.live_trading_ack = live_trading_ack
        self.safe_mode = safe_mode
        self.unknown_reprobe_initial_seconds = max(1, unknown_reprobe_initial_seconds)
        self.unknown_reprobe_max_seconds = max(
            self.unknown_reprobe_initial_seconds,
            unknown_reprobe_max_seconds,
        )
        self.unknown_reprobe_escalation_attempts = max(1, unknown_reprobe_escalation_attempts)
        self.unknown_reprobe_force_observe_only = unknown_reprobe_force_observe_only
        self.unknown_reprobe_force_kill_switch = unknown_reprobe_force_kill_switch
        explicit_env_role = os.getenv("PROCESS_ROLE") or os.getenv("APP_ROLE")
        env_role = get_process_role_from_env().value
        default_role = (
            "LIVE" if live_trading_enabled and explicit_env_role is None else env_role
        )
        self.process_role = coerce_process_role(process_role or default_role).value
        self.unknown_reprobe_max_lookback_seconds = max(
            60 * 60, unknown_reprobe_max_lookback_seconds
        )
        self.pending_recovery_max_attempts = max(1, pending_recovery_max_attempts)
        self.pending_recovery_backoff_seconds = max(1, pending_recovery_backoff_seconds)
        self.pending_grace_seconds = max(0, pending_grace_seconds)
        self.submit_retry_max_attempts = max(1, submit_retry_max_attempts)
        self.cancel_retry_max_attempts = max(1, cancel_retry_max_attempts)
        self.retry_base_delay_ms = max(0, retry_base_delay_ms)
        self.retry_max_delay_ms = max(self.retry_base_delay_ms, retry_max_delay_ms)
        self.spot_sell_requires_inventory = bool(spot_sell_requires_inventory)
        quote_override = os.getenv("EXECUTION_QUOTE_ASSET")
        self.execution_quote_asset_override = (
            quote_override.strip().upper() if quote_override and quote_override.strip() else None
        )
        self.estimated_fee_bps = Decimal(os.getenv("EXECUTION_ESTIMATED_FEE_BPS", "15"))
        self.balance_safety_buffer_ratio = Decimal(
            os.getenv("EXECUTION_BALANCE_SAFETY_BUFFER_RATIO", "0")
        )
        self.sell_fee_in_base_bps = Decimal(os.getenv("EXECUTION_SELL_FEE_IN_BASE_BPS", "0"))
        self.allow_min_notional_upgrade = os.getenv(
            "EXECUTION_ALLOW_MIN_NOTIONAL_UPGRADE", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.fallback_min_notional_try = Decimal(
            os.getenv("EXECUTION_FALLBACK_MIN_NOTIONAL_TRY", "10")
        )
        self.sleep_fn = sleep_fn or time.sleep
        self.execution_wrapper = ExecutionWrapper(
            exchange,
            submit_retry_max_attempts=self.submit_retry_max_attempts,
            cancel_retry_max_attempts=self.cancel_retry_max_attempts,
            retry_base_delay_ms=self.retry_base_delay_ms,
            retry_max_delay_ms=self.retry_max_delay_ms,
            sleep_fn=self.sleep_fn,
        )
        self.lifecycle_refresh_min_interval_seconds = 5
        self.lifecycle_backoff_cooldown_seconds = 15
        self._last_lifecycle_refresh_at: dict[str, float] = {}
        self._lifecycle_backoff_until: float = 0.0
        self.last_lifecycle_refresh_summary: dict[str, object] = {}
        self.last_execute_summary: dict[str, int] = {}
        self._cycle_balance_cache: dict[str, dict[str, Decimal]] = {}
        self._cycle_balance_order: list[str] = []
        self._lifecycle_refreshed_cycles: set[str] = set()
        self._lifecycle_refreshed_order: list[str] = []
        self.unknown_order_registry = UnknownOrderRegistry()
        self._sync_unknown_registry_from_store(allow_clear=True)
        self._emit_unknown_freeze_metrics()

    def _emit_unknown_freeze_metrics(
        self, *, submit_blocked: bool = False, reconcile_run: bool = False
    ) -> None:
        metrics = get_instrumentation()
        metrics.gauge(
            "unknown_order_present",
            1.0 if self.unknown_order_registry.has_unknown() else 0.0,
        )
        metrics.gauge("time_frozen_seconds", self.unknown_order_registry.frozen_seconds())
        if submit_blocked:
            metrics.counter("submit_blocked_due_to_unknown", 1)
        if reconcile_run:
            metrics.counter("reconcile_runs", 1)

    def _sync_unknown_registry_from_store(self, *, allow_clear: bool) -> None:
        records = [
            UnknownOrderRecord(
                order_id=order.order_id,
                reason=(order.exchange_status_raw or "unknown"),
                ts_ms=(order.last_seen_at or int(order.updated_at.timestamp() * 1000)),
            )
            for order in self.state_store.list_unknown_orders()
        ]
        self.unknown_order_registry.sync_snapshot(records, allow_clear=allow_clear)

    def mark_lifecycle_refreshed(self, *, cycle_id: str) -> None:
        if not cycle_id:
            return
        normalized = str(cycle_id)
        if normalized in self._lifecycle_refreshed_cycles:
            if normalized in self._lifecycle_refreshed_order:
                self._lifecycle_refreshed_order.remove(normalized)
        else:
            self._lifecycle_refreshed_cycles.add(normalized)
        self._lifecycle_refreshed_order.append(normalized)
        while len(self._lifecycle_refreshed_order) > MAX_CYCLE_LIFECYCLE_MARKERS:
            evicted = self._lifecycle_refreshed_order.pop(0)
            self._lifecycle_refreshed_cycles.discard(evicted)

    def was_lifecycle_refreshed(self, *, cycle_id: str | None) -> bool:
        return bool(cycle_id and str(cycle_id) in self._lifecycle_refreshed_cycles)

    def _api_degrade_snapshot(self) -> dict[str, object]:
        snapshot_fn = getattr(self.exchange, "health_snapshot", None)
        if callable(snapshot_fn):
            snapshot = snapshot_fn()
            if isinstance(snapshot, dict):
                return snapshot
        return {"degraded": False, "breaker_open": False, "recommended_sleep_seconds": 0.0}

    def _symbol_rules_for_execution(self, symbol: str) -> SymbolRules:
        if self.market_data_service is not None:
            return self.market_data_service.get_symbol_rules(symbol)
        return SymbolRules(
            pair_symbol=symbol,
            price_scale=8,
            quantity_scale=8,
            min_total=self.fallback_min_notional_try,
        )

    def _submission_guarded_by_runtime_state(self) -> bool:
        if self.dry_run:
            return False
        kill_enabled, _reason, _until = self.state_store.get_kill_switch(self.process_role)
        if kill_enabled:
            return True
        policy_blocked = is_trading_blocked_by_policy()
        if policy_blocked:
            logger.warning("submission_blocked_by_policy_rotation_hygiene")
            get_instrumentation().counter("trading_blocked_by_policy_total", 1)
            return True
        snapshot = self._api_degrade_snapshot()
        return bool(snapshot.get("degraded", False) or snapshot.get("breaker_open", False))

    def refresh_order_lifecycle(
        self, symbols: list[str], *, skip_non_essential: bool = False
    ) -> dict[str, object]:
        """Reconcile local lifecycle state against exchange open-orders truth for scoped symbols."""
        normalized_symbols = sorted({normalize_symbol(symbol) for symbol in symbols})
        summary: dict[str, object] = {
            "symbols": normalized_symbols,
            "local_open_unknown": 0,
            "matched_on_exchange": 0,
            "imported_external_open": 0,
            "marked_missing": 0,
            "closed": 0,
            "refresh_skipped_due_to_throttle_count": 0,
            "open_orders_calls_count": 0,
            "all_orders_calls_count": 0,
            "backoff_429_count": 0,
            "skipped_due_to_backoff_cooldown": False,
            "skip_non_essential": bool(skip_non_essential),
            "error_code": "",
            "backoff_endpoints": [],
        }
        if not normalized_symbols:
            self.last_lifecycle_refresh_summary = dict(summary)
            return summary

        self._emit_unknown_freeze_metrics(reconcile_run=True)
        reconcile_failed = False
        self.state_store.heartbeat_instance_lock()
        local_orders = self.state_store.find_open_or_unknown_orders(
            normalized_symbols,
            new_grace_seconds=self.pending_grace_seconds,
            include_new_after_grace=True,
            include_escalated_unknown=True,
        )
        orders_by_symbol: dict[str, list] = {}
        for local in local_orders:
            orders_by_symbol.setdefault(local.symbol, []).append(local)

        summary["local_open_unknown"] = len(local_orders)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        now_mono = time.monotonic()
        if now_mono < self._lifecycle_backoff_until:
            summary["skipped_due_to_backoff_cooldown"] = True
            summary["error_code"] = "EXCHANGE_429_BACKOFF"
            summary["backoff_remaining_seconds"] = max(
                0.0, self._lifecycle_backoff_until - now_mono
            )
            logger.warning("order_reconcile_summary", extra={"extra": summary})
            self.last_lifecycle_refresh_summary = dict(summary)
            self._emit_unknown_freeze_metrics()
            return summary
        for symbol in normalized_symbols:
            symbol_orders = orders_by_symbol.get(symbol, [])
            last_refresh = self._last_lifecycle_refresh_at.get(symbol)
            if (
                not symbol_orders
                and last_refresh is not None
                and (now_mono - last_refresh) < self.lifecycle_refresh_min_interval_seconds
            ):
                summary["refresh_skipped_due_to_throttle_count"] += 1
                continue
            self._last_lifecycle_refresh_at[symbol] = now_mono
            recent: list[OrderSnapshot] | None = None
            due_unknown_orders = [
                local
                for local in symbol_orders
                if local.status == OrderStatus.UNKNOWN
                and self._is_unknown_probe_due(local.unknown_next_probe_at, now_ms)
            ]
            try:
                summary["open_orders_calls_count"] += 1
                open_orders = self.exchange.get_open_orders(symbol)
            except Exception as exc:  # noqa: BLE001
                if self._is_exchange_429_error(exc):
                    summary["backoff_429_count"] += 1
                    endpoints = summary.setdefault("backoff_endpoints", [])
                    if "open_orders" not in endpoints:
                        endpoints.append("open_orders")
                    logger.warning(
                        "EXCHANGE_429_BACKOFF",
                        extra={"extra": {"symbol": symbol, "cycle_scope": "order_lifecycle"}},
                    )
                logger.exception(
                    "Lifecycle refresh failed to load open orders",
                    extra={
                        "extra": {
                            "symbol": symbol,
                            "db_path": self.state_store.db_path_abs,
                            "instance_id": self.state_store.instance_id,
                        }
                    },
                )
                reconcile_failed = True
                continue

            open_snapshots = self._open_items_to_snapshots([*open_orders.bids, *open_orders.asks])
            open_ids = {snapshot.order_id for snapshot in open_snapshots}

            for snapshot in open_snapshots:
                mapped = self._map_exchange_status(snapshot.status)
                is_external_open = self.state_store.get_order(snapshot.order_id) is None
                exchange_status_raw = snapshot.status_raw
                if is_external_open and mapped in {
                    OrderStatus.NEW,
                    OrderStatus.OPEN,
                    OrderStatus.PARTIAL,
                }:
                    exchange_status_raw = "external_open:exchange_reconcile"
                    summary["imported_external_open"] += 1
                self._save_reconciled_snapshot(snapshot, exchange_status_raw=exchange_status_raw)
                self.state_store.update_order_status(
                    order_id=snapshot.order_id,
                    status=mapped,
                    exchange_status_raw=exchange_status_raw or snapshot.status_raw,
                    reconciled=True,
                    last_seen_at=snapshot.update_time or snapshot.timestamp,
                )

            for local in symbol_orders:
                if local.order_id in open_ids:
                    summary["matched_on_exchange"] += 1
                    continue
                open_match = (
                    match_order_by_client_id(open_snapshots, local.client_order_id)
                    if local.client_order_id is not None
                    else None
                )
                if open_match is not None:
                    summary["matched_on_exchange"] += 1
                    mapped_open = self._map_exchange_status(open_match.status)
                    if (
                        local.order_id.startswith("unknown:")
                        and local.order_id != open_match.order_id
                    ):
                        self._emit_reconcile_confirmed(local, open_match.order_id)
                        self.state_store.update_order_status(
                            order_id=local.order_id,
                            status=mapped_open,
                            exchange_status_raw=open_match.status_raw,
                            reconciled=True,
                            last_seen_at=open_match.update_time or open_match.timestamp,
                        )
                    continue

                if local.status == OrderStatus.UNKNOWN and not self._is_unknown_probe_due(
                    local.unknown_next_probe_at, now_ms
                ):
                    continue

                if recent is None:
                    if skip_non_essential:
                        continue
                    try:
                        summary["all_orders_calls_count"] += 1
                        recent = self.exchange.get_all_orders(
                            pair_symbol=symbol,
                            start_ms=self._compute_all_orders_start_ms(
                                now_ms=now_ms, due_unknown_orders=due_unknown_orders
                            ),
                            end_ms=now_ms,
                        )
                    except Exception as exc:  # noqa: BLE001
                        if self._is_exchange_429_error(exc):
                            summary["backoff_429_count"] += 1
                            endpoints = summary.setdefault("backoff_endpoints", [])
                            if "all_orders" not in endpoints:
                                endpoints.append("all_orders")
                            logger.warning(
                                "EXCHANGE_429_BACKOFF",
                                extra={
                                    "extra": {
                                        "symbol": symbol,
                                        "cycle_scope": "order_lifecycle_all_orders",
                                    }
                                },
                            )
                        logger.exception(
                            "Lifecycle refresh failed to load all orders",
                            extra={
                                "extra": {
                                    "symbol": symbol,
                                    "db_path": self.state_store.db_path_abs,
                                    "instance_id": self.state_store.instance_id,
                                }
                            },
                        )
                        reconcile_failed = True
                        continue

                matched = self._match_existing_order(local.order_id, local.client_order_id, recent)
                if matched is None:
                    anchor = local.updated_at or local.created_at
                    age_seconds = int((datetime.now(UTC) - anchor).total_seconds())
                    if local.status == OrderStatus.NEW and age_seconds > self.pending_grace_seconds:
                        self.state_store.update_order_status(
                            order_id=local.order_id,
                            status=OrderStatus.REJECTED,
                            exchange_status_raw="missing_on_exchange_after_grace",
                            reconciled=True,
                            last_seen_at=now_ms,
                        )
                        summary["closed"] += 1
                    elif local.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}:
                        self.state_store.update_order_status(
                            order_id=local.order_id,
                            status=OrderStatus.REJECTED,
                            exchange_status_raw="missing_on_exchange_reconcile",
                            reconciled=True,
                            last_seen_at=now_ms,
                        )
                        summary["marked_missing"] += 1
                        summary["closed"] += 1
                    elif local.status == OrderStatus.UNKNOWN:
                        self._mark_unknown_unresolved(local, now_ms)
                    continue

                mapped = self._map_exchange_status(matched.status)
                self._save_reconciled_snapshot(matched)
                self.state_store.update_order_status(
                    order_id=matched.order_id,
                    status=mapped,
                    exchange_status_raw=matched.status_raw,
                    reconciled=True,
                    last_seen_at=matched.update_time or matched.timestamp,
                )
                summary["matched_on_exchange"] += 1
                if mapped in {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.REJECTED}:
                    summary["closed"] += 1
                if (
                    local.order_id.startswith("unknown:")
                    and local.client_order_id
                    and local.order_id != matched.order_id
                ):
                    self._emit_reconcile_confirmed(local, matched.order_id)
                    self.state_store.update_order_status(
                        order_id=local.order_id,
                        status=mapped,
                        exchange_status_raw=matched.status_raw,
                        reconciled=True,
                        last_seen_at=matched.update_time or matched.timestamp,
                    )

        if summary["backoff_429_count"] > 0:
            summary["error_code"] = "EXCHANGE_429_BACKOFF"
            self._lifecycle_backoff_until = max(
                self._lifecycle_backoff_until,
                now_mono + self.lifecycle_backoff_cooldown_seconds,
            )
        elif summary["marked_missing"] > 0:
            summary["error_code"] = "ORDER_LIFECYCLE_DRIFT"
        logger.info("order_reconcile_summary", extra={"extra": summary})
        self.last_lifecycle_refresh_summary = dict(summary)
        self._sync_unknown_registry_from_store(allow_clear=not reconcile_failed)
        self._emit_unknown_freeze_metrics()
        return summary

    def _remember_cycle_balance_snapshot(
        self, *, cycle_id: str, balances: dict[str, Decimal]
    ) -> None:
        normalized = str(cycle_id)
        self._cycle_balance_cache[normalized] = dict(balances)
        if normalized in self._cycle_balance_order:
            self._cycle_balance_order.remove(normalized)
        self._cycle_balance_order.append(normalized)
        while len(self._cycle_balance_order) > MAX_CYCLE_BALANCE_SNAPSHOTS:
            evicted = self._cycle_balance_order.pop(0)
            self._cycle_balance_cache.pop(evicted, None)

    def prime_cycle_balances(self, *, cycle_id: str, balances: list) -> None:
        if not cycle_id:
            return
        normalized_balances = {
            str(item.asset).upper(): Decimal(str(item.free)) for item in balances
        }
        self._remember_cycle_balance_snapshot(cycle_id=str(cycle_id), balances=normalized_balances)

    def _get_cycle_balances(self, *, cycle_id: str | None) -> dict[str, Decimal] | None:
        if not cycle_id:
            return None
        cached = self._cycle_balance_cache.get(str(cycle_id))
        if cached is not None:
            return dict(cached)
        try:
            fetched = self._balance_by_asset()
        except Exception:  # noqa: BLE001
            logger.exception("balance_precheck_snapshot_failed")
            return None
        self._remember_cycle_balance_snapshot(cycle_id=str(cycle_id), balances=fetched)
        return fetched

    def _balance_by_asset(self) -> dict[str, Decimal]:
        balances = self.exchange.get_balances()
        return {str(item.asset).upper(): Decimal(str(item.free)) for item in balances}

    def _derive_symbol_assets(self, symbol: str) -> tuple[str, str]:
        normalized = normalize_symbol(symbol)
        try:
            base_asset, quote_asset = split_symbol(normalized)
            return str(base_asset).upper(), str(quote_asset).upper()
        except ValueError:
            if self.execution_quote_asset_override:
                quote_asset = self.execution_quote_asset_override
                if normalized.endswith(quote_asset) and len(normalized) > len(quote_asset):
                    return normalized[: -len(quote_asset)], quote_asset
                return normalized, quote_asset
            raise

    def _check_balance_precondition(
        self,
        *,
        balances: dict[str, Decimal] | None,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
    ) -> tuple[bool, str | None, Decimal | None, Decimal | None]:
        base_asset, quote_asset = self._derive_symbol_assets(symbol)
        if balances is None or not balances:
            if side == OrderSide.SELL and self.spot_sell_requires_inventory:
                required = quantity + (quantity * (self.sell_fee_in_base_bps / Decimal("10000")))
                return False, base_asset, required, Decimal("0")
            return True, None, None, None

        if side == OrderSide.BUY:
            asset = self.execution_quote_asset_override or quote_asset
            notional = price * quantity
            estimated_fee = notional * (self.estimated_fee_bps / Decimal("10000"))
            safety_buffer = notional * self.balance_safety_buffer_ratio
            required = notional + estimated_fee + safety_buffer
            available = balances.get(asset, Decimal("0"))
            if available < required:
                return False, asset, required, available
            return True, None, None, None

        asset = base_asset
        required = quantity + (quantity * (self.sell_fee_in_base_bps / Decimal("10000")))
        available = balances.get(asset, Decimal("0"))
        if available < required:
            return False, asset, required, available
        return True, None, None, None

    def _is_known_min_notional_reject(self, exc: Exception) -> bool:
        if not isinstance(exc, ExchangeError):
            return False

        if (
            str(exc.error_code) == "1123"
            and (exc.error_message or "").upper() == "FAILED_MIN_TOTAL_AMOUNT"
        ):
            return True

        haystacks = [
            str(exc.error_code),
            exc.error_message or "",
            exc.response_body or "",
            str(exc),
        ]
        joined = " ".join(haystacks).upper()
        return "1123" in joined and "FAILED_MIN_TOTAL_AMOUNT" in joined

    def _is_exchange_429_error(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response is not None and exc.response.status_code == 429
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return True
        response = getattr(exc, "response", None)
        return bool(getattr(response, "status_code", None) == 429)

    def _emit_reconcile_confirmed(self, local_order, real_order_id: str) -> None:
        emit_decision(
            logger,
            {
                "cycle_id": self._resolve_reconcile_cycle_id(local_order),
                "decision_layer": "execution",
                "reason_code": str(ReasonCode.EXECUTION_RECONCILE_CONFIRMED),
                "action": "SUBMIT",
                "scope": "per_intent",
                "client_order_id": local_order.client_order_id,
                "order_id": real_order_id,
                "previous_order_id": local_order.order_id,
            },
        )

    def _compute_all_orders_start_ms(self, *, now_ms: int, due_unknown_orders: list) -> int:
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
        emit_decision(
            logger,
            {
                "cycle_id": self._resolve_reconcile_cycle_id(local_order),
                "decision_layer": "execution",
                "reason_code": str(ReasonCode.EXECUTION_RECONCILE_UNKNOWN_BOUNDED_EXCEEDED),
                "action": "SUPPRESS",
                "scope": "global",
                "symbol": local_order.symbol,
                "order_id": local_order.order_id,
                "entered_safe_mode": True,
            },
        )

    def _resolve_reconcile_cycle_id(self, local_order) -> str:
        with self.state_store._connect() as conn:
            row = conn.execute(
                """
                SELECT cycle_id
                FROM actions
                WHERE (order_id = ? OR client_order_id = ?)
                  AND cycle_id IS NOT NULL
                  AND cycle_id != ''
                ORDER BY id DESC
                LIMIT 1
                """,
                (local_order.order_id, local_order.client_order_id),
            ).fetchone()
        if row is not None:
            return str(row["cycle_id"])
        return f"reconcile:{datetime.now(UTC).strftime('%Y%m%d')}"

    def _save_reconciled_snapshot(
        self, snapshot: OrderSnapshot, *, exchange_status_raw: str | None = None
    ) -> None:
        side = snapshot.side
        if side is None:
            local_order = self.state_store.get_order(snapshot.order_id)
            if local_order is not None:
                side = OrderSide(local_order.side)
        if side is None:
            logger.warning(
                "reconcile_snapshot_missing_side",
                extra={"extra": {"order_id": snapshot.order_id, "symbol": snapshot.pair_symbol}},
            )
            return
        order = Order(
            order_id=snapshot.order_id,
            client_order_id=snapshot.client_order_id,
            symbol=snapshot.pair_symbol,
            side=side,
            price=snapshot.price,
            quantity=snapshot.quantity,
            status=self._map_exchange_status(snapshot.status),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.state_store.save_order(
            order,
            reconciled=True,
            exchange_status_raw=exchange_status_raw or snapshot.status_raw,
        )

    def cancel_stale_orders(self, cycle_id: str) -> int:
        try:
            open_orders = self.exchange.list_open_orders()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to list open orders")
            return 0

        if self.safe_mode:
            logger.warning("safe_mode_blocks_cancel_write_calls")
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "execution",
                    "reason_code": str(ReasonCode.EXECUTION_SUPPRESS_SAFE_MODE),
                    "action": "SUPPRESS",
                    "scope": "global",
                },
            )
            return 0

        if self.kill_switch:
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "execution",
                    "reason_code": str(ReasonCode.EXECUTION_SUPPRESS_KILL_SWITCH),
                    "action": "SUPPRESS",
                    "scope": "global",
                },
            )
            for order in open_orders:
                logger.info(
                    "Kill switch active; would cancel order",
                    extra={"extra": {"order_id": order.order_id, "symbol": order.symbol}},
                )
            return 0

        if self._submission_guarded_by_runtime_state():
            logger.warning("cancel_suppressed_due_to_runtime_state")
            return 0

        canceled = 0
        self.state_store.prune_expired_idempotency_keys()
        now = datetime.now(UTC)
        for order in open_orders:
            created_at = order.created_at
            if created_at is None or created_at.tzinfo is None:
                logger.warning(
                    "Skipping stale check due to missing/naive timestamp",
                    extra={"extra": {"order_id": order.order_id, "symbol": order.symbol}},
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
            if self._was_submitted_in_cycle(cycle_id=cycle_id, order=order):
                logger.info(
                    "Skipping stale cancel for order submitted in same cycle",
                    extra={"extra": {"cycle_id": cycle_id, "order_id": order.order_id}},
                )
                continue

            if not self.dry_run:
                self._ensure_live_side_effects_allowed(cycle_id=cycle_id)

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
                    extra={"extra": {"order_id": order.order_id, "symbol": order.symbol}},
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
                was_canceled = self.execution_wrapper.cancel_order(
                    order_id=order.order_id,
                )
                get_instrumentation().histogram(
                    "cancel_latency_ms",
                    (datetime.now(UTC) - started).total_seconds() * 1000,
                    attrs={"symbol": order.symbol},
                )
            except Exception:
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

            if isinstance(was_canceled, UncertainResult):
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
                    now_ms = int(datetime.now(UTC).timestamp() * 1000)
                    self.state_store.update_order_status(
                        order_id=order.order_id,
                        status=OrderStatus.UNKNOWN,
                        reconciled=True,
                    )
                    self.unknown_order_registry.mark_unknown(
                        order_id=order.order_id,
                        reason=(outcome.reason or "cancel_reconcile_unknown"),
                        ts=now_ms,
                    )
                    self._emit_unknown_freeze_metrics()
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
        """Execute place-order intents and return only *actual* exchange submissions.

        The return value is the number of orders submitted to the exchange as a side effect
        during this call. Dry-run simulations still record would-place actions and idempotency
        status, but do not contribute to the returned count.
        """
        normalized_intents: list[tuple[OrderIntent, Intent | None]] = []
        for raw in intents:
            if isinstance(raw, Intent):
                if cycle_id is None:
                    raise ValueError("cycle_id is required when executing Stage 3 Intent inputs")
                normalized_intents.append((to_order_intent(raw, cycle_id=cycle_id), raw))
            else:
                normalized_intents.append((raw, None))

        symbols = [intent.symbol for intent, _ in normalized_intents]
        execution_cycle_id = normalized_intents[0][0].cycle_id if normalized_intents else None
        if not self.was_lifecycle_refreshed(cycle_id=execution_cycle_id):
            self.refresh_order_lifecycle(symbols)
            if execution_cycle_id:
                self.mark_lifecycle_refreshed(cycle_id=execution_cycle_id)
        self.state_store.prune_expired_idempotency_keys()
        self.last_execute_summary = {
            "orders_submitted": 0,
            "orders_failed_exchange": 0,
            "rejected_intents": 0,
            "intents_rejected_precheck": 0,
            "attempted_exchange_calls": 0,
            "would_submit_orders": 0,
            "would_submit_notional_try": "0",
            "orders_simulated": 0,
            "rejected_min_notional": 0,
            "would_reject_min_notional": 0,
        }
        self._sync_unknown_registry_from_store(allow_clear=False)
        self._emit_unknown_freeze_metrics()

        if self.safe_mode:
            logger.warning("safe_mode_blocks_submit_write_calls")
            if normalized_intents:
                emit_decision(
                    logger,
                    {
                        "cycle_id": normalized_intents[0][0].cycle_id,
                        "decision_layer": "execution",
                        "reason_code": str(ReasonCode.EXECUTION_SUPPRESS_SAFE_MODE),
                        "action": "SUPPRESS",
                        "scope": "global",
                    },
                )
            return 0

        if self.kill_switch:
            if normalized_intents:
                emit_decision(
                    logger,
                    {
                        "cycle_id": normalized_intents[0][0].cycle_id,
                        "decision_layer": "execution",
                        "reason_code": str(ReasonCode.EXECUTION_SUPPRESS_KILL_SWITCH),
                        "action": "SUPPRESS",
                        "scope": "global",
                    },
                )
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

        if self._submission_guarded_by_runtime_state():
            logger.warning("submission_suppressed_due_to_runtime_state")
            return 0

        if self.unknown_order_registry.has_unknown():
            for intent, raw_intent in normalized_intents:
                self._emit_unknown_freeze_metrics(submit_blocked=True)
                emit_decision(
                    logger,
                    {
                        "cycle_id": intent.cycle_id,
                        "decision_layer": "execution",
                        "reason_code": str(ReasonCode.RISK_BLOCK_UNKNOWN),
                        "action": "REJECT",
                        "scope": "per_intent",
                        "intent_id": (raw_intent.intent_id if raw_intent else None),
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                    },
                )
            logger.warning(
                "submit_blocked_due_to_unknown_order",
                extra={
                    "extra": {
                        "unknown_orders": [
                            record.order_id for record in self.unknown_order_registry.snapshot()
                        ]
                    }
                },
            )
            raise SubmitBlockedDueToUnknownError("submit blocked while unknown orders exist")

        cycle_balances: dict[str, Decimal] | None = None
        if not self.dry_run:
            cycle_balances = self._get_cycle_balances(cycle_id=execution_cycle_id)

        placed = 0
        orders_simulated = 0
        rejected_intents = 0
        intents_rejected_precheck = 0
        orders_failed_exchange = 0
        attempted_exchange_calls = 0
        would_submit_orders = 0
        would_submit_notional_try = Decimal("0")
        rejected_min_notional = 0
        would_reject_min_notional = 0
        for intent, raw_intent in normalized_intents:
            if not self.dry_run:
                self._ensure_live_side_effects_allowed(
                    cycle_id=intent.cycle_id,
                    symbol=intent.symbol,
                    side=intent.side.value,
                    intent_id=(raw_intent.intent_id if raw_intent else None),
                )

            symbol_normalized = normalize_symbol(intent.symbol)
            if self.market_data_service is None and self.dry_run:
                price = Decimal(str(intent.price))
                quantity = Decimal(str(intent.quantity))
                computed_notional_try = price * quantity
            else:
                rules = self._symbol_rules_for_execution(symbol_normalized)
                sizing = size_order_from_notional(
                    desired_notional_try=Decimal(str(intent.notional)),
                    desired_price=Decimal(str(intent.price)),
                    rules=rules,
                    fallback_min_notional_try=self.fallback_min_notional_try,
                    allow_min_notional_upgrade=self.allow_min_notional_upgrade,
                )

                if sizing.status != OrderSizingStatus.OK:
                    if sizing.status == OrderSizingStatus.BELOW_MIN_NOTIONAL:
                        if self.dry_run:
                            would_reject_min_notional += 1
                        else:
                            rejected_min_notional += 1
                    logger.info(
                        "intent_rejected_pre_submit_validation",
                        extra={
                            "extra": {
                                "cycle_id": intent.cycle_id,
                                "intent_id": (raw_intent.intent_id if raw_intent else None),
                                "symbol": symbol_normalized,
                                "side": intent.side.value,
                                "status": sizing.status.value,
                                "reason": sizing.reason,
                                "desired_notional_try": str(intent.notional),
                                "min_notional_try": str(
                                    rules.min_total or self.fallback_min_notional_try
                                ),
                                "computed_notional_try": str(sizing.notional_try),
                            }
                        },
                    )
                    rejected_intents += 1
                    continue

                price = sizing.quantized_price
                quantity = sizing.quantized_quantity
                computed_notional_try = sizing.notional_try

            logger.debug(
                "intent_quantized_pre_submit",
                extra={
                    "extra": {
                        "symbol": symbol_normalized,
                        "side": intent.side.value,
                        "desired_notional_try": str(intent.notional),
                        "computed_notional_try": str(computed_notional_try),
                        "price": str(price),
                        "quantity": str(quantity),
                    }
                },
            )

            if not self.dry_run:
                is_sufficient, asset, required, available = self._check_balance_precondition(
                    balances=cycle_balances,
                    symbol=symbol_normalized,
                    side=intent.side,
                    price=price,
                    quantity=quantity,
                )
                if not is_sufficient:
                    precheck_reason = None
                    if (
                        intent.side == OrderSide.SELL
                        and self.spot_sell_requires_inventory
                        and (cycle_balances is None or not cycle_balances)
                    ):
                        precheck_reason = "balances_missing_fail_closed"
                    missing = (required or Decimal("0")) - (available or Decimal("0"))
                    logger.warning(
                        "execution_reject_insufficient_balance_precheck",
                        extra={
                            "extra": {
                                "cycle_id": intent.cycle_id,
                                "intent_id": (raw_intent.intent_id if raw_intent else None),
                                "symbol": symbol_normalized,
                                "side": intent.side.value,
                                "asset": asset,
                                "required": str(required),
                                "available": str(available),
                                "missing_amount": str(max(missing, Decimal("0"))),
                                "precheck_reason": precheck_reason,
                            }
                        },
                    )
                    emit_decision(
                        logger,
                        {
                            "cycle_id": intent.cycle_id,
                            "decision_layer": "execution",
                            "reason_code": str(
                                ReasonCode.EXECUTION_REJECT_INSUFFICIENT_BALANCE_PRECHECK
                            ),
                            "action": "REJECT",
                            "scope": "per_intent",
                            "intent_id": (raw_intent.intent_id if raw_intent else None),
                            "symbol": intent.symbol,
                            "side": intent.side.value,
                            "asset": asset,
                            "missing_amount": str(max(missing, Decimal("0"))),
                            "precheck_reason": precheck_reason,
                        },
                    )
                    intents_rejected_precheck += 1
                    rejected_intents += 1
                    continue

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
                reservation_age = max(
                    0,
                    int(datetime.now(UTC).timestamp()) - reservation.created_at_epoch,
                )
                if (
                    reservation.status.upper() == "PENDING"
                    and reservation_age > self.pending_grace_seconds
                    and reservation.client_order_id
                ):
                    if self._recover_stale_pending_place_order(intent, reservation):
                        continue
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
            client_order_id = make_client_order_id(intent)
            self.state_store.finalize_idempotency_key(
                "place_order",
                idempotency_key,
                action_id=None,
                client_order_id=client_order_id,
                order_id=None,
                status="PENDING",
            )
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
                client_order_id=client_order_id,
                order_id=None,
                status="PENDING",
            )
            if self.dry_run:
                would_submit_orders += 1
                would_submit_notional_try += computed_notional_try
                emit_decision(
                    logger,
                    {
                        "cycle_id": intent.cycle_id,
                        "decision_layer": "execution",
                        "reason_code": str(ReasonCode.EXECUTION_SUBMIT_DRY_RUN_SIMULATED),
                        "action": "SUBMIT",
                        "scope": "per_intent",
                        "intent_id": (raw_intent.intent_id if raw_intent else None),
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "simulated": True,
                    },
                )
                self.state_store.attach_action_metadata(
                    action_id=action_id,
                    client_order_id=client_order_id,
                    order_id=None,
                    reconciled=False,
                    reconcile_status=None,
                    reconcile_reason=None,
                    idempotency_key=(idempotency_key),
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
                orders_simulated += 1
                continue

            attempted_exchange_calls += 1
            try:
                order = self._submit_limit_order(
                    symbol=symbol_normalized,
                    side=intent.side,
                    price=price,
                    quantity=quantity,
                    client_order_id=client_order_id,
                    cycle_id=intent.cycle_id,
                    intent_id=(raw_intent.intent_id if raw_intent else None),
                )
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, SubmitBlockedDueToUnknownError):
                    raise
                orders_failed_exchange += 1
                known_min_notional_reject = self._is_known_min_notional_reject(exc)
                if known_min_notional_reject:
                    rejected_min_notional += 1
                if not self._is_uncertain_error(exc):
                    response_body = None
                    status_code = None
                    error_code = None
                    error_message = None
                    request_method = None
                    request_path = None
                    request_json = None
                    failure_phase = "pre_http"
                    sanitized_request_payload = {
                        "symbol": symbol_normalized,
                        "side": intent.side.value,
                        "price": str(price),
                        "qty": str(quantity),
                        "client_order_id": client_order_id,
                    }
                    if isinstance(exc, ExchangeError):
                        failure_phase = "http"
                        response_body = (exc.response_body or "")[:2048]
                        status_code = exc.status_code
                        error_code = exc.error_code
                        error_message = exc.error_message
                        request_method = exc.request_method
                        request_path = exc.request_path
                        request_json = exc.request_json
                    logger.error(
                        "exchange_submit_failed",
                        exc_info=True,
                        extra={
                            "extra": {
                                "cycle_id": intent.cycle_id,
                                "intent_id": (raw_intent.intent_id if raw_intent else None),
                                "symbol": symbol_normalized,
                                "side": intent.side.value,
                                "type": "limit",
                                "price": str(price),
                                "quantity": str(quantity),
                                "client_order_id": client_order_id,
                                "idempotency_key": idempotency_key,
                                "failure_phase": failure_phase,
                                "exception_type": type(exc).__name__,
                                "exception_message": str(exc),
                                "sanitized_request_payload": sanitized_request_payload,
                                "request_method": request_method,
                                "request_path": request_path,
                                "request_json": request_json,
                                "response_body": response_body,
                                "status_code": status_code,
                                "error_code": error_code,
                                "error_message": error_message,
                                "known_min_notional_reject": known_min_notional_reject,
                                "computed_notional_try": str(computed_notional_try),
                            }
                        },
                    )
                    rejected_order_id = f"rejected:{client_order_id}:{idempotency_key[:12]}:{int(datetime.now(UTC).timestamp() * 1000)}"
                    self.state_store.save_order(
                        Order(
                            order_id=rejected_order_id,
                            client_order_id=None,
                            symbol=symbol_normalized,
                            side=intent.side,
                            price=price,
                            quantity=quantity,
                            status=OrderStatus.REJECTED,
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                        ),
                        reconciled=True,
                        exchange_status_raw=(response_body or str(exc))[:2048],
                        idempotency_key=idempotency_key,
                        intent_id=(raw_intent.intent_id if raw_intent else None),
                    )
                    emit_decision(
                        logger,
                        {
                            "cycle_id": intent.cycle_id,
                            "decision_layer": "execution",
                            "reason_code": str(ReasonCode.EXECUTION_REJECT_EXCHANGE_SUBMIT_FAILED),
                            "action": "REJECT",
                            "scope": "per_intent",
                            "intent_id": (raw_intent.intent_id if raw_intent else None),
                            "symbol": intent.symbol,
                            "side": intent.side.value,
                        },
                    )
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
                if outcome.status != ReconcileStatus.CONFIRMED or outcome.order_id is None:
                    self.state_store.attach_action_metadata(
                        action_id=action_id,
                        client_order_id=client_order_id,
                        order_id=None,
                        reconciled=False,
                        reconcile_status=outcome.status.value,
                        reconcile_reason=outcome.reason,
                        idempotency_key=(idempotency_key),
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
                    self.state_store.fail_idempotency(
                        idempotency_key, outcome.reason or "submit_reconcile_unknown"
                    )
                    unknown_order_id = f"unknown:{client_order_id}"
                    now_utc = datetime.now(UTC)
                    self.state_store.save_order(
                        Order(
                            order_id=unknown_order_id,
                            client_order_id=client_order_id,
                            symbol=symbol_normalized,
                            side=intent.side,
                            price=price,
                            quantity=quantity,
                            status=OrderStatus.UNKNOWN,
                            created_at=now_utc,
                            updated_at=now_utc,
                        ),
                        reconciled=True,
                        idempotency_key=idempotency_key,
                        intent_id=(raw_intent.intent_id if raw_intent else None),
                    )
                    self.unknown_order_registry.mark_unknown(
                        order_id=unknown_order_id,
                        reason=(outcome.reason or "submit_reconcile_unknown"),
                        ts=int(now_utc.timestamp() * 1000),
                    )
                    self._emit_unknown_freeze_metrics()
                    continue

                order = Order(
                    order_id=outcome.order_id,
                    client_order_id=client_order_id,
                    symbol=symbol_normalized,
                    side=intent.side,
                    price=price,
                    quantity=quantity,
                    status=OrderStatus.NEW,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                self.state_store.save_order(
                    order,
                    reconciled=True,
                    idempotency_key=(idempotency_key),
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
                    idempotency_key=(idempotency_key),
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
                if not self.dry_run:
                    self.state_store.commit_idempotency(idempotency_key, order.order_id)
                placed += 1
                continue

            if isinstance(order, UncertainResult):
                orders_failed_exchange += 1
                outcome = self._reconcile_submit(
                    symbol_normalized=symbol_normalized,
                    side=intent.side,
                    price=price,
                    quantity=quantity,
                    client_order_id=client_order_id,
                )
                if outcome.status != ReconcileStatus.CONFIRMED or outcome.order_id is None:
                    self.state_store.attach_action_metadata(
                        action_id=action_id,
                        client_order_id=client_order_id,
                        order_id=None,
                        reconciled=False,
                        reconcile_status=outcome.status.value,
                        reconcile_reason=outcome.reason,
                        idempotency_key=(idempotency_key),
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
                    self.state_store.fail_idempotency(
                        idempotency_key, outcome.reason or "submit_reconcile_unknown"
                    )
                    unknown_order_id = f"unknown:{client_order_id}"
                    now_utc = datetime.now(UTC)
                    self.state_store.save_order(
                        Order(
                            order_id=unknown_order_id,
                            client_order_id=client_order_id,
                            symbol=symbol_normalized,
                            side=intent.side,
                            price=price,
                            quantity=quantity,
                            status=OrderStatus.UNKNOWN,
                            created_at=now_utc,
                            updated_at=now_utc,
                        ),
                        reconciled=True,
                        idempotency_key=idempotency_key,
                        intent_id=(raw_intent.intent_id if raw_intent else None),
                    )
                    self.unknown_order_registry.mark_unknown(
                        order_id=unknown_order_id,
                        reason=(outcome.reason or "submit_reconcile_unknown"),
                        ts=int(now_utc.timestamp() * 1000),
                    )
                    self._emit_unknown_freeze_metrics()
                    continue
                now_utc = datetime.now(UTC)
                order = Order(
                    order_id=outcome.order_id,
                    client_order_id=client_order_id,
                    symbol=symbol_normalized,
                    side=intent.side,
                    price=price,
                    quantity=quantity,
                    status=OrderStatus.OPEN,
                    created_at=now_utc,
                    updated_at=now_utc,
                )

            if isinstance(order, SubmitOrderResult):
                now_utc = datetime.now(UTC)
                order = Order(
                    order_id=order.order_id,
                    client_order_id=client_order_id,
                    symbol=symbol_normalized,
                    side=intent.side,
                    price=price,
                    quantity=quantity,
                    status=OrderStatus.OPEN,
                    created_at=now_utc,
                    updated_at=now_utc,
                )

            self.state_store.save_order(
                order,
                idempotency_key=(idempotency_key),
                intent_id=(raw_intent.intent_id if raw_intent else None),
            )
            self.state_store.attach_action_metadata(
                action_id=action_id,
                client_order_id=client_order_id,
                order_id=order.order_id,
                reconciled=False,
                reconcile_status=None,
                reconcile_reason=None,
                idempotency_key=(idempotency_key),
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
            if not self.dry_run:
                self.state_store.commit_idempotency(idempotency_key, order.order_id)
            placed += 1
        if placed > 0:
            inc_counter(
                "bot_orders_submitted_total",
                labels={"symbol": "multi", "side": "mixed", "process_role": self.process_role},
                delta=placed,
            )
        if orders_failed_exchange > 0:
            inc_counter(
                "bot_orders_failed_total",
                labels={
                    "symbol": "multi",
                    "reason": "exchange_error",
                    "process_role": self.process_role,
                },
                delta=orders_failed_exchange,
            )

        self.last_execute_summary = {
            "orders_submitted": placed,
            "orders_simulated": orders_simulated,
            "orders_failed_exchange": orders_failed_exchange,
            "rejected_intents": rejected_intents,
            "intents_rejected_precheck": intents_rejected_precheck,
            "attempted_exchange_calls": attempted_exchange_calls,
            "would_submit_orders": would_submit_orders,
            "would_submit_notional_try": str(would_submit_notional_try),
            "rejected_min_notional": rejected_min_notional,
            "would_reject_min_notional": would_reject_min_notional,
        }
        return placed

    def _submit_limit_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str,
        cycle_id: str,
        intent_id: str | None,
    ) -> SubmitOrderResult | UncertainResult:
        self._sync_unknown_registry_from_store(allow_clear=False)
        self._emit_unknown_freeze_metrics()
        get_instrumentation().counter("submit_gate_enforced_total", 1)

        if self.unknown_order_registry.has_unknown():
            self._emit_unknown_freeze_metrics(submit_blocked=True)
            emit_decision(
                logger,
                {
                    "cycle_id": cycle_id,
                    "decision_layer": "execution",
                    "reason_code": str(ReasonCode.RISK_BLOCK_UNKNOWN),
                    "action": "REJECT",
                    "scope": "per_intent",
                    "intent_id": intent_id,
                    "symbol": symbol,
                    "side": side.value,
                },
            )
            logger.warning(
                "submit_blocked_due_to_unknown_order",
                extra={
                    "extra": {
                        "unknown_orders": [
                            record.order_id for record in self.unknown_order_registry.snapshot()
                        ],
                    }
                },
            )
            raise SubmitBlockedDueToUnknownError("submit blocked while unknown orders exist")

        submit_result = self.execution_wrapper.submit_limit_order(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            client_order_id=client_order_id,
        )
        if isinstance(submit_result, UncertainResult):
            return submit_result
        return map_exchange_ack_to_submit_result(submit_result)

    def _recover_stale_pending_place_order(
        self,
        intent: OrderIntent,
        reservation,
    ) -> bool:
        now_epoch = int(datetime.now(UTC).timestamp())
        if (
            reservation.next_recovery_at_epoch is not None
            and now_epoch < reservation.next_recovery_at_epoch
        ):
            return True

        symbol_normalized = normalize_symbol(intent.symbol)
        try:
            open_orders = self.exchange.get_open_orders(symbol_normalized)
            snapshots = self._open_items_to_snapshots([*open_orders.bids, *open_orders.asks])
            matched = match_order_by_client_id(snapshots, reservation.client_order_id)
            if matched is None:
                now_ms = int(datetime.now(UTC).timestamp() * 1000)
                all_orders = self.exchange.get_all_orders(
                    pair_symbol=symbol_normalized,
                    start_ms=now_ms - 5 * 60 * 1000,
                    end_ms=now_ms,
                )
                matched = match_order_by_client_id(all_orders, reservation.client_order_id)
        except Exception:  # noqa: BLE001
            attempts = reservation.recovery_attempts + 1
            max_backoff = self.pending_recovery_backoff_seconds * (
                2 ** (self.pending_recovery_max_attempts - 1)
            )
            backoff_seconds = min(
                self.pending_recovery_backoff_seconds * (2 ** (attempts - 1)),
                max_backoff,
            )
            terminal = attempts >= self.pending_recovery_max_attempts
            self.state_store.update_idempotency_recovery(
                "place_order",
                reservation.key,
                recovery_attempts=attempts,
                next_recovery_at_epoch=(None if terminal else now_epoch + backoff_seconds),
                status=("FAILED" if terminal else "PENDING"),
            )
            get_instrumentation().counter(
                "pending_recovery_lookup_failures_total",
                attrs={"action_type": "place_order"},
            )
            logger.exception(
                "stale pending recovery lookup failed",
                extra={
                    "extra": {
                        "idempotency_key": reservation.key,
                        "recovery_attempts": attempts,
                        "max_attempts": self.pending_recovery_max_attempts,
                    }
                },
            )
            return True

        if matched is not None:
            self.state_store.finalize_idempotency_key(
                "place_order",
                reservation.key,
                action_id=reservation.action_id,
                client_order_id=reservation.client_order_id,
                order_id=matched.order_id,
                status="COMMITTED",
            )
            self.state_store.update_idempotency_recovery(
                "place_order",
                reservation.key,
                recovery_attempts=0,
                next_recovery_at_epoch=None,
            )
            logger.info(
                "stale pending recovered as committed",
                extra={
                    "extra": {
                        "idempotency_key": reservation.key,
                        "client_order_id": reservation.client_order_id,
                        "order_id": matched.order_id,
                    }
                },
            )
            return True

        self.state_store.finalize_idempotency_key(
            "place_order",
            reservation.key,
            action_id=reservation.action_id,
            client_order_id=reservation.client_order_id,
            order_id=None,
            status="FAILED",
        )
        self.state_store.update_idempotency_recovery(
            "place_order",
            reservation.key,
            recovery_attempts=0,
            next_recovery_at_epoch=None,
        )
        logger.info(
            "stale pending marked failed for retry",
            extra={
                "extra": {
                    "idempotency_key": reservation.key,
                    "client_order_id": reservation.client_order_id,
                }
            },
        )
        return True

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
        intent_fingerprint = "|".join(
            [
                normalize_symbol(intent.symbol),
                intent.side.value,
                format(Decimal(str(intent.price)), "f"),
                format(Decimal(str(intent.quantity)), "f"),
            ]
        )
        digest.update(intent_fingerprint.encode("utf-8"))
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
            snapshots = self._open_items_to_snapshots([*open_orders.bids, *open_orders.asks])
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
            matched = self._match_existing_order(order.order_id, order.client_order_id, recent)
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
        category = classify_exchange_error(exc)
        return category in {
            ExecutionErrorCategory.UNCERTAIN,
            ExecutionErrorCategory.TRANSIENT,
            ExecutionErrorCategory.RATE_LIMIT,
        }

    def _was_submitted_in_cycle(self, *, cycle_id: str, order: Order) -> bool:
        with self.state_store._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM actions
                WHERE cycle_id = ?
                  AND action_type = 'place_order'
                  AND (order_id = ? OR client_order_id = ?)
                LIMIT 1
                """,
                (cycle_id, order.order_id, order.client_order_id),
            ).fetchone()
        return row is not None

    def _ensure_live_side_effects_allowed(
        self,
        *,
        cycle_id: str = "",
        symbol: str | None = None,
        side: str | None = None,
        intent_id: str | None = None,
    ) -> None:
        policy = validate_live_side_effects_policy(
            process_role=self.process_role,
            enforce_monitor_role=True,
            dry_run=self.dry_run,
            kill_switch=self.kill_switch,
            live_trading_enabled=self.live_trading_enabled,
            live_trading_ack=self.live_trading_ack,
            cycle_id=cycle_id,
            logger=logger if cycle_id else None,
            decision_layer="policy_gate",
            action="REJECT",
            scope="global",
            symbol=symbol,
            side=side,
            intent_id=intent_id,
        )
        if not policy.allowed:
            raise LiveTradingNotArmedError(
                policy.message,
                reasons=policy.reasons,
                reason_codes=[str(policy_reason_to_code(reason)) for reason in policy.reasons],
            )

    def _canonical_place_fields(self, intent: OrderIntent) -> tuple[str, str, str, str]:
        symbol = normalize_symbol(intent.symbol)
        price = Decimal(str(intent.price))
        quantity = Decimal(str(intent.quantity))
        if self.market_data_service is not None:
            try:
                rules = self.market_data_service.get_symbol_rules(symbol)
                price = quantize_price(price, rules)
                quantity = quantize_quantity(quantity, rules)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "place_hash_quantization_skipped",
                    extra={"extra": {"symbol": symbol}},
                )
        return symbol, intent.side.value, format(price, "f"), format(quantity, "f")

    def _stable_place_hash(self, intent: OrderIntent) -> str:
        symbol, side, price, quantity = self._canonical_place_fields(intent)
        raw = "|".join([symbol, side, price, quantity])
        return hashlib.sha256(raw.encode()).hexdigest()

    def _place_hash(self, intent: OrderIntent) -> str:
        return self._stable_place_hash(intent)

    def _cancel_hash(self, order_id: str) -> str:
        return hashlib.sha256(order_id.encode()).hexdigest()
