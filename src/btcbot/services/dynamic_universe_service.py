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
_STABLE_TOKENS = {
    "USDT",
    "USDC",
    "DAI",
    "TUSD",
    "FDUSD",
    "BUSD",
    "USDP",
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
    observed_at: datetime | None


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

        filters = {
            "top_n": settings.universe_top_n,
            "spread_max_bps": str(settings.universe_spread_max_bps),
            "min_depth_try": str(settings.universe_min_depth_try),
            "exclude_stables": settings.universe_exclude_stables,
            "exclude_symbols": list(settings.universe_exclude_symbols),
            "orderbook_max_age_seconds": settings.universe_orderbook_max_age_seconds,
            "churn_max_per_day": settings.universe_churn_max_per_day,
        }
        ineligible_counts: dict[str, int] = {}
        candidates: list[_Candidate] = []

        excluded = {canonical_symbol(item) for item in settings.universe_exclude_symbols}
        now_bucket = self._bucket_ts(now_utc, settings.universe_history_bucket_minutes)

        t_books = perf_counter()
        for symbol in symbols:
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

            metrics = self._fetch_orderbook_metrics(exchange, symbol)
            if metrics is None:
                self._inc(ineligible_counts, "orderbook_unavailable")
                continue
            if metrics.observed_at is None:
                self._inc(ineligible_counts, "orderbook_no_timestamp")
                instr.counter("orderbook_no_timestamp", 1)
                continue
            age_seconds = (now_utc - ensure_utc(metrics.observed_at)).total_seconds()
            if age_seconds < 0 or age_seconds > settings.universe_orderbook_max_age_seconds:
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
        instr.histogram("universe_fetch_orderbooks_ms", (perf_counter() - t_books) * 1000.0)

        t_score = perf_counter()
        ranked = sorted(candidates, key=lambda item: (Decimal("0") - item.score, item.symbol))
        desired = tuple(item.symbol for item in ranked[: max(0, settings.universe_top_n)])
        previous = tuple(str(item) for item in (latest or {}).get("selected_symbols", []))
        churn_count = self._compute_churn_count(previous, desired)
        day_churn = state_store.get_dynamic_universe_churn_count_for_day(now_utc)
        selected = desired
        if churn_count > 0 and day_churn >= settings.universe_churn_max_per_day:
            viable_previous = tuple(sym for sym in previous if any(c.symbol == sym for c in ranked))
            if viable_previous:
                selected = viable_previous[: max(0, settings.universe_top_n)]
                self._inc(ineligible_counts, "churn_guard")
                churn_count = 0
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
            refreshed=True,
        )

        for reason, count in ineligible_counts.items():
            instr.counter(f"ineligible_{reason}", count)
        instr.counter("universe_selected", len(selected))
        instr.gauge("universe_selected_count", float(len(selected)))
        instr.gauge("universe_churn_count", float(churn_count))

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
                    "churn_count": churn_count,
                    "refreshed": True,
                }
            },
        )
        return DynamicUniverseSelection(
            selected_symbols=selected,
            scores=score_map,
            filters=filters,
            ineligible_counts=ineligible_counts,
            refreshed=True,
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

    def _fetch_orderbook_metrics(self, exchange: object, symbol: str) -> _OrderbookMetrics | None:
        base = getattr(exchange, "client", exchange)
        getter = getattr(base, "get_orderbook_with_timestamp", None)
        if callable(getter):
            try:
                data = getter(symbol)
                parsed = self._parse_timestamped_orderbook(data)
                if parsed is not None:
                    return parsed
            except Exception:  # noqa: BLE001
                pass

        get_raw = getattr(base, "_get", None)
        if callable(get_raw):
            try:
                payload = get_raw("/api/v2/orderbook", params={"pairSymbol": symbol})
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict):
                    parsed = self._parse_raw_orderbook(data, payload)
                    if parsed is not None:
                        return parsed
            except Exception:  # noqa: BLE001
                pass

        get_orderbook = getattr(base, "get_orderbook", None)
        if not callable(get_orderbook):
            return None
        try:
            bid_raw, ask_raw = get_orderbook(symbol)
            bid = Decimal(str(bid_raw))
            ask = Decimal(str(ask_raw))
        except Exception:  # noqa: BLE001
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / Decimal("2")
        spread_bps = ((ask - bid) / mid) * _BPS
        return _OrderbookMetrics(mid_price=mid, spread_bps=spread_bps, depth_try=None, observed_at=None)

    def _parse_timestamped_orderbook(self, data: object) -> _OrderbookMetrics | None:
        if isinstance(data, dict):
            return self._parse_raw_orderbook(data, data)
        if isinstance(data, tuple) and len(data) >= 3:
            bid = Decimal(str(data[0]))
            ask = Decimal(str(data[1]))
            observed_at = self._parse_timestamp(data[2])
            if bid <= 0 or ask <= 0 or ask < bid:
                return None
            mid = (bid + ask) / Decimal("2")
            spread_bps = ((ask - bid) / mid) * _BPS
            return _OrderbookMetrics(mid_price=mid, spread_bps=spread_bps, depth_try=None, observed_at=observed_at)
        return None

    def _parse_raw_orderbook(
        self, data: dict[str, object], payload: dict[str, object] | None = None
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
        observed_at = self._parse_timestamp(data.get("timestamp"))
        if observed_at is None and isinstance(payload, dict):
            observed_at = self._parse_timestamp(payload.get("timestamp"))
        if observed_at is None and isinstance(payload, dict):
            observed_at = self._parse_timestamp(payload.get("serverTime"))
        mid = (bid_price + ask_price) / Decimal("2")
        spread_bps = ((ask_price - bid_price) / mid) * _BPS
        depth_try = (bid_qty * bid_price) + (ask_qty * ask_price)
        return _OrderbookMetrics(
            mid_price=mid,
            spread_bps=spread_bps,
            depth_try=depth_try,
            observed_at=observed_at,
        )

    @staticmethod
    def _parse_timestamp(raw: object) -> datetime | None:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 1_000_000_000_000:
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=UTC)
        if isinstance(raw, str):
            text = raw.strip()
            if text.isdigit():
                return DynamicUniverseService._parse_timestamp(int(text))
            try:
                parsed = datetime.fromisoformat(text)
                return ensure_utc(parsed)
            except ValueError:
                return None
        return None

    @staticmethod
    def _bucket_ts(ts: datetime, bucket_minutes: int) -> datetime:
        minute = (ts.minute // bucket_minutes) * bucket_minutes
        return ts.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def _is_stable_symbol(symbol: str) -> bool:
        base = symbol[:-3] if symbol.endswith("TRY") else symbol
        return base in _STABLE_TOKENS or any(token in symbol for token in _STABLE_TOKENS)

    @staticmethod
    def _is_cooldown(state: dict[str, object], now_utc: datetime) -> bool:
        cooldown_until = _parse_optional_ts(state.get("cooldown_until_ts"))
        return cooldown_until is not None and cooldown_until > now_utc

    @staticmethod
    def _is_probation(state: dict[str, object], now_utc: datetime) -> bool:
        probation_until = _parse_optional_ts(state.get("probation_until_ts"))
        last_selected_ts = _parse_optional_ts(state.get("last_selected_ts"))
        return probation_until is not None and probation_until > now_utc and last_selected_ts is None

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
    def _compute_churn_count(previous: tuple[str, ...], current: tuple[str, ...]) -> int:
        return len(set(previous).symmetric_difference(set(current)))

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
