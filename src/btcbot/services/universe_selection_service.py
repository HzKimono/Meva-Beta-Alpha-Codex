from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import sqrt
from typing import TYPE_CHECKING

from btcbot.domain.symbols import canonical_symbol, quote_currency
from btcbot.domain.universe import UniverseCandidate, UniverseSelectionResult
from btcbot.services.state_store import StateStore

if TYPE_CHECKING:
    from btcbot.config import Settings


logger = logging.getLogger(__name__)
_BPS = Decimal("10000")
_MISSING_METRIC_PENALTY = Decimal("-1")
_DEFAULT_WEIGHT_LIQUIDITY = Decimal("0.50")
_DEFAULT_WEIGHT_SPREAD = Decimal("0.30")
_DEFAULT_WEIGHT_VOLATILITY = Decimal("0.20")


@dataclass(frozen=True)
class _RawMetrics:
    volume_try: Decimal | None
    spread_bps: Decimal | None
    volatility: Decimal | None
    age_sec: float | None


class UniverseSelectionService:
    def select_universe(
        self, *, exchange: object, settings: Settings, now_utc: datetime
    ) -> UniverseSelectionResult:
        now_utc = self._ensure_utc(now_utc)
        store = StateStore(db_path=settings.state_db_path)
        role = "STAGE7"

        previous = store.get_latest_stage7_universe_snapshot(role=role) or {}
        prev_symbols = [canonical_symbol(str(x)) for x in previous.get("selected_symbols", [])]

        freeze_reasons = self._freeze_reasons(exchange=exchange)
        exchange_info = self._safe_get_exchange_info(exchange)
        symbols, discovery_exclusions = self._discover_symbols(
            exchange_info=exchange_info,
            settings=settings,
        )

        ticker_stats = self._fetch_ticker_stats(exchange)
        raw_metrics: dict[str, _RawMetrics] = {}
        stale_detected = False
        for symbol in symbols:
            volume_try = self._extract_quote_volume_try(symbol=symbol, ticker_stats=ticker_stats)
            spread_bps, age_sec = self._fetch_spread_bps_and_age(
                exchange=exchange,
                symbol=symbol,
                now_utc=now_utc,
            )
            volatility = self._fetch_volatility(
                exchange=exchange,
                symbol=symbol,
                settings=settings,
                ticker_stats=ticker_stats,
            )
            if age_sec is None or age_sec > settings.stage7_max_data_age_sec:
                stale_detected = True
            raw_metrics[symbol] = _RawMetrics(
                volume_try=volume_try,
                spread_bps=spread_bps,
                volatility=volatility,
                age_sec=age_sec,
            )
            if age_sec is None or age_sec > settings.stage7_max_data_age_sec:
                stale_detected = True
            raw_metrics[symbol] = _RawMetrics(volume_try, spread_bps, volatility, age_sec)

        if stale_detected:
            freeze_reasons.append("stale_market_data")

        scored_all = self._score_candidates(raw_metrics=raw_metrics, settings=settings)
        eligible: list[UniverseCandidate] = []
        for candidate in scored_all:
            reasons = self._candidate_exclusions(candidate=candidate, settings=settings)
            if reasons:
                for reason in reasons:
                    excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
                continue
            eligible.append(candidate)

        governed = self._apply_governance(
            store=store,
            eligible=eligible,
            previous=prev_symbols,
            settings=settings,
            now_utc=now_utc,
        )
        selected = governed[: max(0, settings.stage7_universe_size)]

        if stale_detected:
            freeze_reasons.append("STALE_DATA")

        scored = self._score_candidates(raw_metrics=raw_metrics, settings=settings)
        eligible: list[UniverseCandidate] = []
        excluded_counts = dict(discovery_exclusions)
        for candidate in scored:
            candidate_reasons = self._candidate_exclusions(candidate=candidate, settings=settings)
            for reason in candidate_reasons:
                excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
            if candidate_reasons:
                continue
            eligible.append(candidate)

        selected = self._apply_governance(
            store=store,
            eligible=eligible,
            previous=prev_symbols,
            settings=settings,
            now_utc=now_utc,
            role=role,
        )

        freeze = bool(freeze_reasons) and bool(prev_symbols)
        if freeze:
            selected = prev_symbols[: max(0, settings.stage7_universe_size)]

        selected_set = set(selected)
        selected_scored = [item for item in scored if item.symbol in selected_set]

        additions = sorted(set(selected) - set(prev_symbols))
        removals = sorted(set(prev_symbols) - set(selected))
        churn_total = len(additions) + len(removals)
        excluded_counts["churn_additions"] = len(additions)
        excluded_counts["churn_removals"] = len(removals)
        excluded_counts["churn_total"] = churn_total

        if stale_detected:
            freeze_reasons.append("STALE_DATA")

        scored = self._score_candidates(raw_metrics=raw_metrics, settings=settings)
        eligible: list[UniverseCandidate] = []
        excluded_counts = dict(discovery_exclusions)
        for candidate in scored:
            candidate_reasons = self._candidate_exclusions(candidate=candidate, settings=settings)
            for reason in candidate_reasons:
                excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
            if candidate_reasons:
                continue
            eligible.append(candidate)

        selected = self._apply_governance(
            store=store,
            eligible=eligible,
            previous=prev_symbols,
            settings=settings,
            now_utc=now_utc,
            role=role,
        )

        freeze = bool(freeze_reasons) and bool(prev_symbols)
        if freeze:
            selected = prev_symbols[: max(0, settings.stage7_universe_size)]

        selected_set = set(selected)
        selected_scored = [item for item in scored if item.symbol in selected_set]

        additions = sorted(set(selected) - set(prev_symbols))
        removals = sorted(set(prev_symbols) - set(selected))
        churn_total = len(additions) + len(removals)
        excluded_counts["churn_additions"] = len(additions)
        excluded_counts["churn_removals"] = len(removals)
        excluded_counts["churn_total"] = churn_total

        if stale_detected:
            freeze_reasons.append("STALE_DATA")

        scored = self._score_candidates(raw_metrics=raw_metrics, settings=settings)
        eligible: list[UniverseCandidate] = []
        excluded_counts = dict(discovery_exclusions)
        for candidate in scored:
            candidate_reasons = self._candidate_exclusions(candidate=candidate, settings=settings)
            for reason in candidate_reasons:
                excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
            if candidate_reasons:
                continue
            eligible.append(candidate)

        selected = self._apply_governance(
            store=store,
            eligible=eligible,
            previous=prev_symbols,
            settings=settings,
            now_utc=now_utc,
            role=role,
        )

        freeze = bool(freeze_reasons) and bool(prev_symbols)
        if freeze:
            selected = prev_symbols[: max(0, settings.stage7_universe_size)]

        selected_set = set(selected)
        selected_scored = [item for item in scored if item.symbol in selected_set]

        additions = sorted(set(selected) - set(prev_symbols))
        removals = sorted(set(prev_symbols) - set(selected))
        churn_total = len(additions) + len(removals)
        excluded_counts["churn_additions"] = len(additions)
        excluded_counts["churn_removals"] = len(removals)
        excluded_counts["churn_total"] = churn_total

        reasons = [
            "deterministic_ranking: total desc -> liquidity desc -> symbol asc",
            "missing_metrics_penalty: -1 applied per missing score component",
            "dry_run_read_only: universe selection never places orders",
        ]
        if freeze:
            reasons.append("freeze_previous_universe")
        reasons.extend(self._humanize_freeze_reasons(freeze_reasons))

        store.save_stage7_universe_snapshot(
            role=role,
            ts=now_utc,
            selected_symbols=selected,
            scored=[self._candidate_to_json(item) for item in selected_scored],
            reasons=reasons,
            freeze_reason=(freeze_reasons[0] if freeze_reasons else None),
            freeze_reasons=freeze_reasons,
            excluded_counts=excluded_counts,
            churn_count=churn_total,
        )

        self._log_selection(
            freeze=freeze,
            freeze_reason=(freeze_reasons[0] if freeze_reasons else None),
            candidate_count=len(symbols),
            selected_count=len(selected),
            excluded_counts=excluded_counts,
            churn_additions=len(additions),
            churn_removals=len(removals),
            churn_total=churn_total,
        )

        return UniverseSelectionResult(
            selected_symbols=selected,
            scored=selected_scored,
            reasons=reasons,
            freeze_reason=(freeze_reasons[0] if freeze_reasons else None),
            freeze_reasons=freeze_reasons,
            excluded_counts=excluded_counts,
            churn_count=churn_total,
        )

    def _discover_symbols(
        self,
        *,
        exchange_info: list[object],
        settings: Settings,
    ) -> tuple[list[str], dict[str, int]]:
        allow = {canonical_symbol(s) for s in settings.stage7_universe_whitelist}
        deny = {canonical_symbol(s) for s in settings.stage7_universe_blacklist}
        exclusions: dict[str, int] = {}

        out: list[str] = []
        for pair in exchange_info:
            raw_symbol = getattr(pair, "pair_symbol", None) or getattr(pair, "pairSymbol", None)
            if not raw_symbol:
                exclusions["excluded_by_symbol_missing"] = (
                    exclusions.get("excluded_by_symbol_missing", 0) + 1
                )
                continue
            symbol = canonical_symbol(str(raw_symbol))
            if quote_currency(symbol) != "TRY":
                exclusions["excluded_by_quote"] = exclusions.get("excluded_by_quote", 0) + 1
                continue
            if allow and symbol not in allow:
                exclusions["excluded_by_allowlist"] = exclusions.get("excluded_by_allowlist", 0) + 1
                continue
            if symbol in deny:
                exclusions["excluded_by_denylist"] = exclusions.get("excluded_by_denylist", 0) + 1
                continue
            eligible, reason = self._metadata_eligible(pair)
            if not eligible:
                key = reason or "excluded_by_metadata"
                exclusions[key] = exclusions.get(key, 0) + 1
                continue
            out.append(symbol)
        return sorted(set(out)), exclusions

    @staticmethod
    def _candidate_to_json(item: UniverseCandidate) -> dict[str, object]:
        return {
            "symbol": item.symbol,
            "liquidity_score": str(item.liquidity_score),
            "spread_score": str(item.spread_score),
            "volatility_score": str(item.volatility_score),
            "total_score": str(item.total_score),
            "breakdown": dict(item.breakdown),
        }

    def _apply_governance(
        self,
        *,
        store: StateStore,
        eligible: list[UniverseCandidate],
        previous: list[str],
        settings: Settings,
        now_utc: datetime,
        role: str,
    ) -> list[str]:
        cooldown_sec = settings.stage7_universe_governance_cooldown_sec
        probation_cycles = settings.stage7_universe_governance_probation_cycles
        max_size = max(0, settings.stage7_universe_size)
        desired = [item.symbol for item in eligible[:max_size]]
        previous_set = set(previous)

        filtered: list[str] = []
        for symbol in desired:
            state = store.get_stage7_universe_symbol_state(symbol) or {}
            cooldown_until = self._parse_optional_dt(state.get("cooldown_until_ts"))
            if (
                cooldown_until is not None
                and now_utc < cooldown_until
                and symbol not in previous_set
            ):
                continue
            passes = int(state.get("probation_passes") or 0)
            if symbol not in previous_set:
                passes += 1
                if passes < probation_cycles:
                    store.upsert_stage7_universe_symbol_state(
                        symbol=symbol,
                        updated_at=now_utc,
                        probation_passes=passes,
                        last_seen_ts=now_utc,
                        last_added_ts=self._parse_optional_dt(state.get("last_added_ts")),
                        last_removed_ts=self._parse_optional_dt(state.get("last_removed_ts")),
                        cooldown_until_ts=cooldown_until,
                    )
                    continue
            store.upsert_stage7_universe_symbol_state(
                symbol=symbol,
                updated_at=now_utc,
                probation_passes=passes,
                last_seen_ts=now_utc,
                last_added_ts=(
                    now_utc
                    if symbol not in previous_set
                    else self._parse_optional_dt(state.get("last_added_ts"))
                ),
                last_removed_ts=self._parse_optional_dt(state.get("last_removed_ts")),
                cooldown_until_ts=cooldown_until,
            )
            filtered.append(symbol)

        additions = [x for x in filtered if x not in previous_set]
        removals = [x for x in previous if x not in set(filtered)]
        churn_total = len(additions) + len(removals)
        churn_24h = store.get_stage7_universe_churn_count_since(
            role=role,
            since_utc=now_utc - timedelta(hours=24),
        )
        if (
            churn_24h + churn_total > settings.stage7_universe_governance_max_churn_per_day
            and previous
        ):
            return previous[:max_size]

        for symbol in removals:
            state = store.get_stage7_universe_symbol_state(symbol) or {}
            store.upsert_stage7_universe_symbol_state(
                symbol=symbol,
                updated_at=now_utc,
                probation_passes=0,
                last_seen_ts=self._parse_optional_dt(state.get("last_seen_ts")),
                last_added_ts=self._parse_optional_dt(state.get("last_added_ts")),
                last_removed_ts=now_utc,
                cooldown_until_ts=now_utc + timedelta(seconds=cooldown_sec),
            )

        return filtered

    @staticmethod
    def _freeze_reasons(*, exchange: object) -> list[str]:
        reasons: list[str] = []
        health = getattr(exchange, "health_snapshot", None)
        if callable(health):
            try:
                snapshot = health()
            except Exception:  # noqa: BLE001
                snapshot = {}
            if bool(dict(snapshot).get("degraded")):
                reasons.append("EXCHANGE_DEGRADED")
        if bool(getattr(exchange, "observe_only", False)):
            reasons.append("OBSERVE_ONLY")
        return reasons

    @staticmethod
    def _humanize_freeze_reasons(keys: list[str]) -> list[str]:
        mapping = {
            "STALE_DATA": "stale_market_data",
            "OBSERVE_ONLY": "observe_only",
            "EXCHANGE_DEGRADED": "exchange_degraded",
        }
        return [mapping[item] for item in sorted(set(keys)) if item in mapping]

    @staticmethod
    def _safe_get_exchange_info(exchange: object) -> list[object]:
        getter = getattr(exchange, "get_exchange_info", None)
        if not callable(getter):
            return []
        try:
            rows = getter()
        except Exception:  # noqa: BLE001
            return []
        return list(rows)

    @staticmethod
    def _metadata_eligible(pair: object) -> tuple[bool, str | None]:
        fields = {
            "min_notional": (
                getattr(pair, "min_total_amount", None),
                getattr(pair, "minTotalAmount", None),
            ),
            "min_qty": (
                getattr(pair, "minimum_order_amount", None),
                getattr(pair, "minimumOrderAmount", None),
                getattr(pair, "min_quantity", None),
                getattr(pair, "minQuantity", None),
            ),
            "step_size": (
                getattr(pair, "denominator_scale", None),
                getattr(pair, "denominatorScale", None),
            ),
        }
        raw_notional = next((x for x in fields["min_notional"] if x is not None), None)
        raw_min_qty = next((x for x in fields["min_qty"] if x is not None), None)
        raw_step = next((x for x in fields["step_size"] if x is not None), None)

        min_notional = UniverseSelectionService._as_decimal(raw_notional)
        min_qty = UniverseSelectionService._as_decimal(raw_min_qty)
        step_size = UniverseSelectionService._as_decimal(raw_step)

        has_metadata_attrs = any(
            hasattr(pair, name)
            for name in (
                "min_total_amount",
                "minTotalAmount",
                "minimum_order_amount",
                "minimumOrderAmount",
                "min_quantity",
                "minQuantity",
                "denominator_scale",
                "denominatorScale",
            )
        )

        if min_notional is None and min_qty is None and step_size is None:
            if has_metadata_attrs:
                return False, "excluded_by_metadata_missing"
            return True, None
        if min_notional is not None and min_notional <= 0:
            return False, "excluded_by_min_notional"
        if min_qty is not None and min_qty <= 0:
            return False, "excluded_by_min_qty"
        if step_size is not None and step_size < 0:
            return False, "excluded_by_step_size"
        return True, None

    def _fetch_ticker_stats(self, exchange: object) -> dict[str, dict[str, Decimal]]:
        getter = getattr(exchange, "get_ticker_stats", None)
        if not callable(getter):
            return {}
        try:
            rows = getter()
        except Exception:  # noqa: BLE001
            return {}

        parsed: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol_raw = row.get("pairSymbol") or row.get("pair") or row.get("symbol")
            if symbol_raw is None:
                continue
            symbol = canonical_symbol(str(symbol_raw))
            parsed[symbol] = {
                "volume": self._as_decimal(row.get("volume") or row.get("quoteVolume")),
                "last": self._as_decimal(row.get("last") or row.get("lastPrice") or row.get("close")),
                "high": self._as_decimal(row.get("high") or row.get("highPrice")),
                "low": self._as_decimal(row.get("low") or row.get("lowPrice")),
                "price_change": self._as_decimal(row.get("priceChangePercent") or row.get("dailyPercent")),
            }
        return parsed

    @staticmethod
    def _extract_quote_volume_try(
        *, symbol: str, ticker_stats: dict[str, dict[str, Decimal]]
    ) -> Decimal | None:
        stats = ticker_stats.get(symbol)
        if stats is None:
            return None
        return stats.get("volume")

    def _fetch_spread_bps_and_age(
        self,
        *,
        exchange: object,
        symbol: str,
        now_utc: datetime,
    ) -> tuple[Decimal | None, float | None]:
        getter_ts = getattr(exchange, "get_orderbook_with_timestamp", None)
        if callable(getter_ts):
            try:
                bid, ask, ts = getter_ts(symbol)
                spread = self.compute_spread_bps(
                    best_bid=Decimal(str(bid)),
                    best_ask=Decimal(str(ask)),
                )
                if ts is None:
                    return spread, None
                age_sec = max(0.0, (now_utc - self._ensure_utc(ts)).total_seconds())
                return spread, age_sec
            except Exception:  # noqa: BLE001
                return None, None

        getter = getattr(exchange, "get_orderbook", None)
        if not callable(getter):
            return None, None
        try:
            bid, ask = getter(symbol)
            spread = self.compute_spread_bps(best_bid=Decimal(str(bid)), best_ask=Decimal(str(ask)))
            return spread, None
        except Exception:  # noqa: BLE001
            return None, None

    @staticmethod
    def compute_spread_bps(*, best_bid: Decimal, best_ask: Decimal) -> Decimal | None:
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None
        mid = (best_bid + best_ask) / Decimal("2")
        if mid <= 0:
            return None
        return ((best_ask - best_bid) / mid) * _BPS

    def _fetch_volatility(
        self,
        *,
        exchange: object,
        symbol: str,
        settings: Settings,
        ticker_stats: dict[str, dict[str, Decimal]],
    ) -> Decimal | None:
        closes = self._fetch_candle_closes(
            exchange=exchange,
            symbol=symbol,
            lookback=max(2, settings.stage7_vol_lookback),
        )
        if len(closes) >= 2:
            return self._compute_return_std(closes)

        stats = ticker_stats.get(symbol)
        if stats is None:
            return None
        last = stats.get("last")
        high = stats.get("high")
        low = stats.get("low")
        if last is not None and high is not None and low is not None and last > 0 and high >= low:
            return (high - low) / last
        change = stats.get("price_change")
        if change is None:
            return None
        return change.copy_abs() / Decimal("100")

    def _fetch_candle_closes(
        self,
        *,
        exchange: object,
        symbol: str,
        lookback: int,
    ) -> list[Decimal]:
        getter = getattr(exchange, "get_candles", None)
        if not callable(getter):
            return []
        try:
            rows = getter(symbol, lookback)
        except Exception:  # noqa: BLE001
            return []

        closes: list[Decimal] = []
        for row in rows:
            if isinstance(row, dict):
                close_val = self._as_decimal(row.get("close") or row.get("c"))
            else:
                close_val = self._as_decimal(getattr(row, "close", None))
            if close_val is None or close_val <= 0:
                continue
            closes.append(close_val)
        return closes

    @staticmethod
    def _compute_return_std(closes: Sequence[Decimal]) -> Decimal:
        returns: list[float] = []
        for prev, curr in zip(closes[:-1], closes[1:], strict=False):
            if prev <= 0:
                continue
            returns.append(float((curr - prev) / prev))
        if not returns:
            return Decimal("0")
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / len(returns)
        return Decimal(str(sqrt(variance)))

    @staticmethod
    def _as_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _parse_optional_dt(raw: object) -> datetime | None:
        if raw in {None, ""}:
            return None
        parsed = datetime.fromisoformat(str(raw))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _candidate_exclusions(
        self, *, candidate: UniverseCandidate, settings: Settings
    ) -> list[str]:
        reasons: list[str] = []
        spread_raw = candidate.breakdown.get("spread_bps", "missing")
        volume_raw = candidate.breakdown.get("volume_try", "missing")

        if spread_raw == "missing":
            reasons.append("excluded_by_missing_spread")
        else:
            spread = Decimal(str(spread_raw))
            if settings.stage7_max_spread_bps > 0 and spread > settings.stage7_max_spread_bps:
                reasons.append("excluded_by_max_spread_bps")

        if volume_raw == "missing":
            reasons.append("excluded_by_missing_volume")
        else:
            volume = Decimal(str(volume_raw))
            if volume < settings.stage7_min_quote_volume_try:
                reasons.append("excluded_by_min_quote_volume_try")

        return reasons

    def _score_candidates(
        self,
        *,
        raw_metrics: dict[str, _RawMetrics],
        settings: Settings,
    ) -> list[UniverseCandidate]:
        weights = self._resolve_weights(settings)
        liquidity_values = [item.volume_try for item in raw_metrics.values() if item.volume_try is not None]
        spread_values = [item.spread_bps for item in raw_metrics.values() if item.spread_bps is not None]
        volatility_values = [item.volatility for item in raw_metrics.values() if item.volatility is not None]

        liquidity_min, liquidity_max = self._bounds(liquidity_values)
        spread_min, spread_max = self._bounds(spread_values)
        volatility_min, volatility_max = self._bounds(volatility_values)

        scored: list[UniverseCandidate] = []
        for symbol, metrics in raw_metrics.items():
            liquidity_score = self._normalize_linear(metrics.volume_try, liquidity_min, liquidity_max)
            spread_score = self._normalize_inverse(metrics.spread_bps, spread_min, spread_max)
            volatility_score = self._normalize_inverse(
                metrics.volatility, volatility_min, volatility_max
            )

            total_score = (
                liquidity_score * weights["liquidity"]
                + spread_score * weights["spread"]
                + volatility_score * weights["volatility"]
            )

            scored.append(
                UniverseCandidate(
                    symbol=symbol,
                    liquidity_score=liquidity_score,
                    spread_score=spread_score,
                    volatility_score=volatility_score,
                    total_score=total_score,
                    breakdown={
                        "liquidity_score": float(liquidity_score),
                        "spread_score": float(spread_score),
                        "volatility_score": float(volatility_score),
                        "total_score": float(total_score),
                        "volume_try": (
                            "missing" if metrics.volume_try is None else float(metrics.volume_try)
                        ),
                        "spread_bps": (
                            "missing" if metrics.spread_bps is None else float(metrics.spread_bps)
                        ),
                        "volatility": (
                            "missing" if metrics.volatility is None else float(metrics.volatility)
                        ),
                        "age_sec": "missing" if metrics.age_sec is None else float(metrics.age_sec),
                    },
                )
            )

        scored.sort(key=lambda item: (-item.total_score, item.symbol))
        return scored

    @staticmethod
    def _resolve_weights(settings: Settings) -> dict[str, Decimal]:
        configured = settings.stage7_score_weights or {}
        liquidity = Decimal(str(configured.get("liquidity", _DEFAULT_WEIGHT_LIQUIDITY)))
        spread = Decimal(str(configured.get("spread", _DEFAULT_WEIGHT_SPREAD)))
        volatility = Decimal(str(configured.get("volatility", _DEFAULT_WEIGHT_VOLATILITY)))
        total = liquidity + spread + volatility
        if total <= 0:
            return {
                "liquidity": _DEFAULT_WEIGHT_LIQUIDITY,
                "spread": _DEFAULT_WEIGHT_SPREAD,
                "volatility": _DEFAULT_WEIGHT_VOLATILITY,
            }
        return {
            "liquidity": liquidity / total,
            "spread": spread / total,
            "volatility": volatility / total,
        }

    @staticmethod
    def _bounds(values: list[Decimal]) -> tuple[Decimal, Decimal]:
        if not values:
            return Decimal("0"), Decimal("0")
        return min(values), max(values)

    @staticmethod
    def _normalize_linear(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal:
        if value is None:
            return _MISSING_METRIC_PENALTY
        if high <= low:
            return Decimal("1")
        return (value - low) / (high - low)

    @staticmethod
    def _normalize_inverse(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal:
        if value is None:
            return _MISSING_METRIC_PENALTY
        if high <= low:
            return Decimal("1")
        return Decimal("1") - ((value - low) / (high - low))

    @staticmethod
    def _log_selection(
        *,
        freeze: bool,
        freeze_reason: str | None,
        candidate_count: int,
        selected_count: int,
        excluded_counts: dict[str, int],
        churn_additions: int,
        churn_removals: int,
        churn_total: int,
    ) -> None:
        payload: dict[str, object] = {
            "universe.selected_count": selected_count,
            "universe.candidate_count": candidate_count,
            "universe.freeze": freeze,
            "universe.freeze_reason": freeze_reason,
            "universe.churn.additions": churn_additions,
            "universe.churn.removals": churn_removals,
            "universe.churn.total": churn_total,
        }
        for reason, count in sorted(excluded_counts.items()):
            payload[f"universe.excluded.{reason}"] = count
        logger.info("stage7_universe_selected", extra={"extra": payload})
