from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import log
from time import perf_counter

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.domain.symbols import canonical_symbol
from btcbot.observability import get_instrumentation
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)
_BPS = Decimal("10000")
_EMPTY_SELECTION_LOG_THRESHOLD = 2
_STABLE_TOKENS = {
    "USDT",
    "USDC",
    "DAI",
    "TUSD",
    "FDUSD",
    "BUSD",
}


@dataclass(frozen=True)
class DynamicUniverseSelection:
    selected_symbols: tuple[str, ...]
    scores: dict[str, Decimal]
    filters: dict[str, object]
    ineligible_counts: dict[str, int]
    refreshed: bool


@dataclass(frozen=True)
class _OrderbookMetrics:
    mid_price: Decimal
    spread_bps: Decimal
    depth_try: Decimal | None
    observed_at: datetime


@dataclass(frozen=True)
class _Candidate:
    symbol: str
    momentum_24h: Decimal
    spread_bps: Decimal
    depth_try: Decimal
    reject_penalty: Decimal
    score: Decimal


class DynamicUniverseService:
    def select(
        self,
        *,
        exchange: object,
        state_store: StateStore,
        settings: Settings,
        now_utc: datetime,
        cycle_id: str,
    ) -> DynamicUniverseSelection:
        now_utc = ensure_utc(now_utc)
        if not settings.dynamic_universe_enabled:
            return DynamicUniverseSelection((), {}, {}, {}, refreshed=False)

        instr = get_instrumentation()
        latest = state_store.get_latest_dynamic_universe_selection()
        refresh_delta = timedelta(minutes=settings.universe_refresh_minutes)
        if latest is not None:
            ts = ensure_utc(datetime.fromisoformat(str(latest["ts"])))
            if now_utc - ts < refresh_delta:
                selected = tuple(str(item) for item in latest.get("selected_symbols", []))
                score_map = {
                    str(symbol): Decimal(str(score))
                    for symbol, score in dict(latest.get("scores", {})).items()
                }
                filters = dict(latest.get("filters", {}))
                ineligible = {
                    str(reason): int(count)
                    for reason, count in dict(latest.get("ineligible_counts", {})).items()
                }
                state_store.save_dynamic_universe_selection(
                    cycle_id=cycle_id,
                    ts=now_utc,
                    selected_symbols=list(selected),
                    scores={key: str(val) for key, val in score_map.items()},
                    score_breakdown=latest.get("score_breakdown", {}),
                    filters=filters,
                    ineligible_counts=ineligible,
                    churn_count=int(latest.get("churn_count", 0)),
                    refreshed=False,
                )
                return DynamicUniverseSelection(
                    selected_symbols=selected,
                    scores=score_map,
                    filters=filters,
                    ineligible_counts=ineligible,
                    refreshed=False,
                )

        t0 = perf_counter()
        symbols = self._fetch_try_pairs(exchange)
        instr.histogram("universe_fetch_pairs_ms", (perf_counter() - t0) * 1000.0)
        instr.counter("universe_candidates", len(symbols))

        scan_budget_symbols = max(
            int(settings.universe_top_n),
            int(settings.universe_scan_budget_symbols),
        )
        orderbook_request_cap = max(1, int(settings.universe_max_orderbook_requests_per_cycle))

        filters = {
            "top_n": settings.universe_top_n,
            "spread_max_bps": str(settings.universe_spread_max_bps),
            "min_depth_try": str(settings.universe_min_depth_try),
            "exclude_stables": settings.universe_exclude_stables,
            "exclude_symbols": list(settings.universe_exclude_symbols),
            "orderbook_max_age_seconds": settings.universe_orderbook_max_age_seconds,
            "churn_max_per_day": settings.universe_churn_max_per_day,
            "scan_budget_symbols": scan_budget_symbols,
            "orderbook_request_cap": orderbook_request_cap,
        }
        ineligible_counts: dict[str, int] = {}
        diagnostics: dict[str, int] = {}
        candidates: list[_Candidate] = []

        excluded = {canonical_symbol(item) for item in settings.universe_exclude_symbols}
        now_bucket = self._bucket_ts(now_utc, settings.universe_history_bucket_minutes)
        orderbook_requests = 0
        scanned_symbols = symbols[:scan_budget_symbols]
        skipped_for_scan_budget = max(0, len(symbols) - len(scanned_symbols))

        t_books = perf_counter()
        for symbol in scanned_symbols:
            symbol_state = state_store.get_dynamic_universe_symbol_state(symbol) or {}
            if self._is_cooldown(symbol_state, now_utc):
                self._inc(ineligible_counts, "cooldown")
                continue
            if self._is_probation(symbol_state, now_utc):
                self._inc(ineligible_counts, "probation")
                continue
            if symbol in excluded:
                self._inc(ineligible_counts, "excluded_symbol")
                continue
            if settings.universe_exclude_stables and self._is_stable_symbol(symbol):
                self._inc(ineligible_counts, "stable_symbol")
                continue

            if orderbook_requests >= orderbook_request_cap:
                self._inc(ineligible_counts, "scan_budget_exhausted")
                continue

            orderbook_requests += 1
            metrics = self._fetch_orderbook_metrics(
                exchange,
                symbol,
                cycle_id=cycle_id,
                diagnostics=diagnostics,
            )
            if metrics is None:
                self._inc(ineligible_counts, "orderbook_unavailable")
                continue
            age_seconds = max(0.0, (now_utc - ensure_utc(metrics.observed_at)).total_seconds())
            if age_seconds > settings.universe_orderbook_max_age_seconds:
                self._inc(ineligible_counts, "stale_orderbook")
                instr.counter("stale_orderbook", 1)
                continue
            if metrics.depth_try is None:
                self._inc(ineligible_counts, "depth_unavailable")
                instr.counter("depth_unavailable", 1)
                continue

            state_store.upsert_universe_price_snapshot(
                pair_symbol=symbol,
                ts_bucket=now_bucket,
                mid_price=metrics.mid_price,
            )
            if metrics.spread_bps > settings.universe_spread_max_bps:
                self._inc(ineligible_counts, "spread_too_wide")
                continue
            if metrics.depth_try < settings.universe_min_depth_try:
                self._inc(ineligible_counts, "insufficient_depth")
                continue

            lookback_price = state_store.get_universe_price_lookback(
                pair_symbol=symbol,
                target_ts=now_utc - timedelta(hours=24),
                tolerance=timedelta(minutes=settings.universe_history_tolerance_minutes),
            )
            if lookback_price is None or lookback_price <= 0:
                self._inc(ineligible_counts, "insufficient_history")
                continue

            momentum_24h = (metrics.mid_price / lookback_price) - Decimal("1")
            reject_counts = self._reject_counts(symbol_state, now_utc, settings)
            reject_penalty = Decimal(str(settings.universe_reject_penalty_weight)) * Decimal(
                str(reject_counts.get("1123", 0))
            )
            if reject_counts.get("1123", 0) >= settings.universe_reject_1123_threshold:
                self._inc(ineligible_counts, "reject_1123")
                state_store.upsert_dynamic_universe_symbol_state(
                    symbol=symbol,
                    updated_at=now_utc,
                    cooldown_until_ts=now_utc
                    + timedelta(minutes=settings.universe_symbol_cooldown_minutes),
                    last_selected_ts=_parse_optional_ts(symbol_state.get("last_selected_ts")),
                    probation_until_ts=_parse_optional_ts(symbol_state.get("probation_until_ts")),
                    reject_window_start_ts=_parse_optional_ts(
                        symbol_state.get("reject_window_start_ts")
                    ),
                    reject_counts=reject_counts,
                )
                continue

            depth_component = Decimal(str(log(float(metrics.depth_try + Decimal("1")))))
            score = (
                (settings.universe_score_weight_momentum * momentum_24h)
                - (settings.universe_score_weight_spread * metrics.spread_bps)
                + (settings.universe_score_weight_depth * depth_component)
                - reject_penalty
            )
            candidates.append(
                _Candidate(
                    symbol=symbol,
                    momentum_24h=momentum_24h,
                    spread_bps=metrics.spread_bps,
                    depth_try=metrics.depth_try,
                    reject_penalty=reject_penalty,
                    score=score,
                )
            )
        if skipped_for_scan_budget > 0:
            ineligible_counts["scan_budget_exhausted"] = (
                ineligible_counts.get("scan_budget_exhausted", 0) + skipped_for_scan_budget
            )
        diagnostics.setdefault("timestamp_parse_fail_count", 0)
        diagnostics["depth_unavailable_count"] = ineligible_counts.get("depth_unavailable", 0)
        diagnostics["orderbook_unavailable_count"] = ineligible_counts.get("orderbook_unavailable", 0)
        diagnostics["stale_filtered_count"] = ineligible_counts.get("stale_orderbook", 0)
        if diagnostics:
            filters["diagnostics"] = dict(sorted(diagnostics.items()))
        instr.counter("universe_orderbook_requests_per_cycle", orderbook_requests)
        instr.histogram("universe_fetch_orderbooks_ms", (perf_counter() - t_books) * 1000.0)

        t_score = perf_counter()
        ranked = sorted(candidates, key=lambda item: (Decimal("0") - item.score, item.symbol))
        desired = tuple(item.symbol for item in ranked[: max(0, settings.universe_top_n)])
        previous = tuple(str(item) for item in (latest or {}).get("selected_symbols", []))
        selected = desired
        guarded = False
        would_change = selected != previous
        day_churn = state_store.get_dynamic_universe_churn_count_for_day(now_utc)
        churn_next = day_churn + (1 if would_change else 0)
        if previous and churn_next > settings.universe_churn_max_per_day:
            # Churn is tracked as selection changes per day (+1 per changed cycle), not symbol swaps.
            selected = previous
            guarded = True
            self._inc(ineligible_counts, "churn_guard")
        churn_count = 1 if selected != previous else 0
        instr.histogram("universe_score_ms", (perf_counter() - t_score) * 1000.0)

        selected_set = set(selected)
        score_map = {item.symbol: item.score for item in ranked if item.symbol in selected_set}
        score_breakdown = {
            item.symbol: {
                "momentum_24h": str(item.momentum_24h),
                "spread_bps": str(item.spread_bps),
                "depth_try": str(item.depth_try),
                "reject_penalty": str(item.reject_penalty),
                "score": str(item.score),
            }
            for item in ranked
            if item.symbol in selected_set
        }

        for symbol in selected:
            symbol_state = state_store.get_dynamic_universe_symbol_state(symbol) or {}
            probation_until = _parse_optional_ts(symbol_state.get("probation_until_ts"))
            if probation_until is None:
                probation_until = now_utc + timedelta(minutes=settings.universe_probation_minutes)
            state_store.upsert_dynamic_universe_symbol_state(
                symbol=symbol,
                updated_at=now_utc,
                last_selected_ts=now_utc,
                cooldown_until_ts=_parse_optional_ts(symbol_state.get("cooldown_until_ts")),
                probation_until_ts=probation_until,
                reject_window_start_ts=_parse_optional_ts(symbol_state.get("reject_window_start_ts")),
                reject_counts=dict(symbol_state.get("reject_counts", {})),
            )

        state_store.save_dynamic_universe_selection(
            cycle_id=cycle_id,
            ts=now_utc,
            selected_symbols=list(selected),
            scores={key: str(val) for key, val in score_map.items()},
            score_breakdown=score_breakdown,
            filters=filters,
            ineligible_counts=ineligible_counts,
            churn_count=churn_count,
            refreshed=not guarded,
        )

        for reason, count in ineligible_counts.items():
            instr.counter(f"ineligible_{reason}", count)
        instr.counter("universe_selected", len(selected))
        instr.gauge("universe_selected_count", float(len(selected)))
        instr.gauge("universe_churn_count", float(churn_count))

        if len(selected) <= _EMPTY_SELECTION_LOG_THRESHOLD:
            logger.info(
                "dynamic_universe_empty",
                extra={
                    "extra": {
                        "selected": len(selected),
                        "candidates": len(scanned_symbols),
                        "parse_fail": diagnostics.get("timestamp_parse_fail_count", 0),
                        "depth_unavail": ineligible_counts.get("depth_unavailable", 0),
                        "orderbook_unavail": ineligible_counts.get("orderbook_unavailable", 0),
                        "stale": ineligible_counts.get("stale_orderbook", 0),
                    }
                },
            )

        logger.info(
            "dynamic_universe_selected",
            extra={
                "extra": {
                    "cycle_id": cycle_id,
                    "selected_symbols": list(selected),
                    "scores": {key: str(val) for key, val in score_map.items()},
                    "score_breakdown": score_breakdown,
                    "filters": filters,
                    "ineligible_counts": ineligible_counts,
                    "diagnostics": diagnostics,
                    "churn_count": churn_count,
                    "refreshed": not guarded,
                    "orderbook_requests": orderbook_requests,
                }
            },
        )
        return DynamicUniverseSelection(
            selected_symbols=selected,
            scores=score_map,
            filters=filters,
            ineligible_counts=ineligible_counts,
            refreshed=not guarded,
        )

    def _fetch_try_pairs(self, exchange: object) -> list[str]:
        base = getattr(exchange, "client", exchange)
        getter = getattr(base, "get_exchange_info", None)
        if not callable(getter):
            return []
        pairs: list[str] = []
        for item in getter():
            if isinstance(item, PairInfo):
                pair_symbol = item.pair_symbol
            else:
                pair_symbol = getattr(item, "pair_symbol", None) or getattr(
                    item, "pairSymbol", None
                )
            if not pair_symbol:
                continue
            normalized = canonical_symbol(str(pair_symbol))
            if normalized.endswith("TRY"):
                pairs.append(normalized)
        return sorted(set(pairs))

    def _fetch_orderbook_metrics(
        self,
        exchange: object,
        symbol: str,
        *,
        cycle_id: str,
        diagnostics: dict[str, int],
    ) -> _OrderbookMetrics | None:
        base = getattr(exchange, "client", exchange)
        instr = get_instrumentation()
        getter = self._resolve_method(exchange, "get_orderbook_with_timestamp")
        if callable(getter):
            try:
                fetched_at = datetime.now(UTC)
                data = getter(symbol)
                parsed = self._parse_timestamped_orderbook(
                    data,
                    fallback_observed_at=fetched_at,
                    diagnostics=diagnostics,
                )
                if parsed is not None:
                    return parsed
            except Exception:  # noqa: BLE001
                self._inc(diagnostics, "orderbook_parse_failure_timestamped")
                instr.counter("universe_orderbook_parse_failure_timestamped", 1)
                logger.debug(
                    "dynamic_universe_orderbook_parse_failed",
                    extra={
                        "extra": {
                            "symbol": symbol,
                            "cycle_id": cycle_id,
                            "branch": "get_orderbook_with_timestamp",
                        }
                    },
                    exc_info=True,
                )

        get_raw = getattr(base, "_get", None)
        if callable(get_raw) and not callable(getter):
            try:
                fetched_at = datetime.now(UTC)
                payload = get_raw("/api/v2/orderbook", params={"pairSymbol": symbol})
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict):
                    parsed = self._parse_raw_orderbook(
                        data,
                        payload,
                        fallback_observed_at=fetched_at,
                        diagnostics=diagnostics,
                    )
                    if parsed is not None:
                        return parsed
            except Exception:  # noqa: BLE001
                self._inc(diagnostics, "orderbook_parse_failure_raw")
                instr.counter("universe_orderbook_parse_failure_raw", 1)
                logger.debug(
                    "dynamic_universe_orderbook_parse_failed",
                    extra={
                        "extra": {
                            "symbol": symbol,
                            "cycle_id": cycle_id,
                            "branch": "_get_orderbook_raw",
                        }
                    },
                    exc_info=True,
                )

        get_orderbook = self._resolve_method(exchange, "get_orderbook")
        if not callable(get_orderbook):
            return None
        try:
            fetched_at = datetime.now(UTC)
            bid_raw, ask_raw = get_orderbook(symbol)
            bid = Decimal(str(bid_raw))
            ask = Decimal(str(ask_raw))
        except Exception:  # noqa: BLE001
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / Decimal("2")
        spread_bps = ((ask - bid) / mid) * _BPS
        return _OrderbookMetrics(
            mid_price=mid,
            spread_bps=spread_bps,
            depth_try=None,
            observed_at=fetched_at,
        )

    def _parse_timestamped_orderbook(
        self,
        data: object,
        *,
        fallback_observed_at: datetime,
        diagnostics: dict[str, int],
    ) -> _OrderbookMetrics | None:
        if isinstance(data, dict):
            return self._parse_raw_orderbook(
                data,
                data,
                fallback_observed_at=fallback_observed_at,
                diagnostics=diagnostics,
            )
        if isinstance(data, tuple) and len(data) >= 3:
            bid = Decimal(str(data[0]))
            ask = Decimal(str(data[1]))
            observed_at = (
                self._parse_timestamp(data[2], diagnostics=diagnostics) or fallback_observed_at
            )
            if bid <= 0 or ask <= 0 or ask < bid:
                return None
            mid = (bid + ask) / Decimal("2")
            spread_bps = ((ask - bid) / mid) * _BPS
            return _OrderbookMetrics(
                mid_price=mid,
                spread_bps=spread_bps,
                depth_try=None,
                observed_at=ensure_utc(observed_at),
            )
        return None

    def _parse_raw_orderbook(
        self,
        data: dict[str, object],
        payload: dict[str, object] | None = None,
        *,
        fallback_observed_at: datetime,
        diagnostics: dict[str, int],
    ) -> _OrderbookMetrics | None:
        bids = data.get("bids")
        asks = data.get("asks")
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            return None
        best_bid = bids[0]
        best_ask = asks[0]
        if not isinstance(best_bid, list) or not isinstance(best_ask, list):
            return None
        if len(best_bid) < 2 or len(best_ask) < 2:
            return None
        try:
            bid_price = Decimal(str(best_bid[0]))
            bid_qty = Decimal(str(best_bid[1]))
            ask_price = Decimal(str(best_ask[0]))
            ask_qty = Decimal(str(best_ask[1]))
        except Exception:  # noqa: BLE001
            return None
        if bid_price <= 0 or ask_price <= 0 or ask_price < bid_price or bid_qty < 0 or ask_qty < 0:
            return None
        observed_at = self._parse_timestamp(data.get("timestamp"), diagnostics=diagnostics)
        if observed_at is None and isinstance(payload, dict):
            observed_at = self._parse_timestamp(payload.get("timestamp"), diagnostics=diagnostics)
        if observed_at is None and isinstance(payload, dict):
            observed_at = self._parse_timestamp(payload.get("serverTime"), diagnostics=diagnostics)
        if observed_at is None:
            observed_at = fallback_observed_at
        mid = (bid_price + ask_price) / Decimal("2")
        spread_bps = ((ask_price - bid_price) / mid) * _BPS
        depth_try = (bid_qty * bid_price) + (ask_qty * ask_price)
        return _OrderbookMetrics(
            mid_price=mid,
            spread_bps=spread_bps,
            depth_try=depth_try,
            observed_at=ensure_utc(observed_at),
        )

    @staticmethod
    def _resolve_method(exchange: object, method_name: str) -> object:
        direct = getattr(exchange, method_name, None)
        if callable(direct):
            return direct
        base = getattr(exchange, "client", None)
        return getattr(base, method_name, None)

    @staticmethod
    def _parse_timestamp(raw: object, *, diagnostics: dict[str, int] | None = None) -> datetime | None:
        def _mark_parse_failure() -> None:
            if diagnostics is not None:
                diagnostics["timestamp_parse_fail_count"] = (
                    diagnostics.get("timestamp_parse_fail_count", 0) + 1
                )

        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            try:
                value = float(raw)
                if value > 1_000_000_000_000:
                    value /= 1000.0
                return datetime.fromtimestamp(value, tz=UTC)
            except (OverflowError, OSError, ValueError):
                _mark_parse_failure()
                return None
        if isinstance(raw, str):
            text = raw.strip()
            if text.isdigit():
                return DynamicUniverseService._parse_timestamp(int(text), diagnostics=diagnostics)
            try:
                parsed = datetime.fromisoformat(text)
                return ensure_utc(parsed)
            except ValueError:
                _mark_parse_failure()
                return None
        return None

    @staticmethod
    def _bucket_ts(ts: datetime, bucket_minutes: int) -> datetime:
        minute = (ts.minute // bucket_minutes) * bucket_minutes
        return ts.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def _is_stable_symbol(symbol: str) -> bool:
        # Stable exclusion is based on parsed base asset, avoiding substring false positives.
        base = symbol[:-3] if symbol.endswith("TRY") else symbol
        return base in _STABLE_TOKENS

    @staticmethod
    def _is_cooldown(state: dict[str, object], now_utc: datetime) -> bool:
        cooldown_until = _parse_optional_ts(state.get("cooldown_until_ts"))
        return cooldown_until is not None and cooldown_until > now_utc

    @staticmethod
    def _is_probation(state: dict[str, object], now_utc: datetime) -> bool:
        probation_until = _parse_optional_ts(state.get("probation_until_ts"))
        return probation_until is not None and probation_until > now_utc

    @staticmethod
    def _reject_counts(
        state: dict[str, object], now_utc: datetime, settings: Settings
    ) -> dict[str, int]:
        counts = {str(k): int(v) for k, v in dict(state.get("reject_counts", {})).items()}
        start = _parse_optional_ts(state.get("reject_window_start_ts"))
        if start is None or now_utc - start > timedelta(minutes=settings.universe_reject_window_minutes):
            return {}
        return counts

    @staticmethod
    def _inc(target: dict[str, int], key: str) -> None:
        target[key] = target.get(key, 0) + 1


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _parse_optional_ts(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(raw)))
    except ValueError:
        return None
