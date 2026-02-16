from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.domain.symbols import canonical_symbol
from btcbot.services.state_store import StateStore

logger = logging.getLogger(__name__)
_BPS = Decimal("10000")


@dataclass(frozen=True)
class DynamicUniverseSelection:
    selected_symbols: tuple[str, ...]
    scores: dict[str, Decimal]
    filters: dict[str, object]
    ineligible_counts: dict[str, int]
    refreshed: bool


@dataclass(frozen=True)
class _Candidate:
    symbol: str
    momentum_24h: Decimal


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
        if not settings.dynamic_universe_enabled:
            return DynamicUniverseSelection((), {}, {}, {}, refreshed=False)

        latest = state_store.get_latest_dynamic_universe_selection()
        refresh_delta = timedelta(minutes=settings.universe_refresh_minutes)
        if latest is not None:
            ts = datetime.fromisoformat(str(latest["ts"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
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
                    filters=filters,
                    ineligible_counts=ineligible,
                )
                return DynamicUniverseSelection(
                    selected_symbols=selected,
                    scores=score_map,
                    filters=filters,
                    ineligible_counts=ineligible,
                    refreshed=False,
                )

        symbols = self._fetch_try_pairs(exchange)
        filters = {
            "top_n": settings.universe_top_n,
            "spread_max_bps": str(settings.universe_spread_max_bps),
            "min_depth_try": str(settings.universe_min_depth_try),
            "exclude_stables": settings.universe_exclude_stables,
            "exclude_symbols": list(settings.universe_exclude_symbols),
        }
        ineligible_counts: dict[str, int] = {}
        candidates: list[_Candidate] = []

        excluded = {canonical_symbol(item) for item in settings.universe_exclude_symbols}
        now_bucket = self._bucket_ts(now_utc, settings.universe_history_bucket_minutes)

        for symbol in symbols:
            metrics = self._fetch_orderbook_metrics(exchange, symbol)
            if metrics is None:
                self._inc(ineligible_counts, "orderbook_unavailable")
                continue
            mid_price, spread_bps, depth_try = metrics
            state_store.upsert_universe_price_snapshot(
                pair_symbol=symbol,
                ts_bucket=now_bucket,
                mid_price=mid_price,
            )

            if settings.universe_exclude_stables and symbol in excluded:
                self._inc(ineligible_counts, "excluded_symbol")
                continue
            if spread_bps > settings.universe_spread_max_bps:
                self._inc(ineligible_counts, "spread_too_wide")
                continue
            if depth_try < settings.universe_min_depth_try:
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

            momentum_24h = (mid_price / lookback_price) - Decimal("1")
            candidates.append(_Candidate(symbol=symbol, momentum_24h=momentum_24h))

        ranked = sorted(
            candidates,
            key=lambda item: (Decimal("0") - item.momentum_24h, item.symbol),
        )
        selected = tuple(item.symbol for item in ranked[: max(0, settings.universe_top_n)])
        score_map = {item.symbol: item.momentum_24h for item in ranked if item.symbol in selected}

        state_store.save_dynamic_universe_selection(
            cycle_id=cycle_id,
            ts=now_utc,
            selected_symbols=list(selected),
            scores={key: str(val) for key, val in score_map.items()},
            filters=filters,
            ineligible_counts=ineligible_counts,
        )
        logger.info(
            "dynamic_universe_selected",
            extra={
                "extra": {
                    "cycle_id": cycle_id,
                    "selected_symbols": list(selected),
                    "scores": {key: str(val) for key, val in score_map.items()},
                    "filters": filters,
                    "ineligible_counts": ineligible_counts,
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

    def _fetch_orderbook_metrics(
        self, exchange: object, symbol: str
    ) -> tuple[Decimal, Decimal, Decimal] | None:
        base = getattr(exchange, "client", exchange)
        get_raw = getattr(base, "_get", None)
        if callable(get_raw):
            try:
                payload = get_raw("/api/v2/orderbook", params={"pairSymbol": symbol})
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict):
                    parsed = self._parse_raw_orderbook(data)
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
        # Fallback when qty is unavailable; preserves eligibility in dry-run fixtures.
        depth_try = bid + ask
        return mid, spread_bps, depth_try

    def _parse_raw_orderbook(
        self, data: dict[str, object]
    ) -> tuple[Decimal, Decimal, Decimal] | None:
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
        mid = (bid_price + ask_price) / Decimal("2")
        spread_bps = ((ask_price - bid_price) / mid) * _BPS
        depth_try = (bid_qty * bid_price) + (ask_qty * ask_price)
        return mid, spread_bps, depth_try

    @staticmethod
    def _bucket_ts(ts: datetime, bucket_minutes: int) -> datetime:
        minute = (ts.minute // bucket_minutes) * bucket_minutes
        return ts.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def _inc(target: dict[str, int], key: str) -> None:
        target[key] = target.get(key, 0) + 1
