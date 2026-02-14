from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from btcbot.adapters.exchange import ExchangeClient
from btcbot.config import Settings
from btcbot.domain.stage4 import ExchangeRules

logger = logging.getLogger(__name__)


def _norm_symbol(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def _pair_symbol_candidates(pair: object) -> list[str]:
    if isinstance(pair, Mapping):
        candidates = [
            pair.get("pairSymbol"),
            pair.get("symbol"),
            pair.get("name"),
            pair.get("nameNormalized"),
            pair.get("name_normalized"),
        ]
        return [candidate for candidate in candidates if isinstance(candidate, str) and candidate]

    candidates = [
        getattr(pair, "pair_symbol", None),
        getattr(pair, "symbol", None),
        getattr(pair, "name", None),
        getattr(pair, "name_normalized", None),
        getattr(pair, "nameNormalized", None),
    ]
    return [candidate for candidate in candidates if isinstance(candidate, str) and candidate]


def _read_field(pair: object, *names: str) -> object:
    if isinstance(pair, Mapping):
        for name in names:
            if name in pair:
                return pair[name]
        return None
    for name in names:
        if hasattr(pair, name):
            return getattr(pair, name)
    return None


def _to_decimal(value: object) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _infer_precision(value: Decimal | None, default: int = 8) -> int:
    if value is None or value <= 0:
        return default
    exponent = value.normalize().as_tuple().exponent
    return max(0, -int(exponent))


@dataclass(frozen=True)
class SymbolRules:
    tick_size: Decimal
    lot_size: Decimal
    min_notional_try: Decimal
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    price_precision: int = 8
    qty_precision: int = 8

    @property
    def step_size(self) -> Decimal:
        return self.lot_size


@dataclass
class _CachedRules:
    rules: SymbolRules
    status: str
    cached_at: datetime


class ExchangeRulesService:
    def __init__(
        self,
        exchange: ExchangeClient,
        *,
        cache_ttl_sec: int = 300,
        settings: Settings | None = None,
    ) -> None:
        self.exchange = exchange
        self.cache_ttl_sec = max(1, cache_ttl_sec)
        self.settings = settings
        self._cache: dict[str, _CachedRules] = {}

    def _fallback_rules(self) -> SymbolRules:
        tick_size = Decimal(
            str(getattr(self.settings, "stage7_rules_fallback_tick_size", Decimal("0.01")))
        )
        lot_size = Decimal(
            str(getattr(self.settings, "stage7_rules_fallback_lot_size", Decimal("0.00000001")))
        )
        min_notional = Decimal(
            str(getattr(self.settings, "stage7_rules_fallback_min_notional_try", Decimal("10")))
        )
        return SymbolRules(
            tick_size=tick_size,
            lot_size=lot_size,
            min_notional_try=min_notional,
            min_qty=None,
            max_qty=None,
            price_precision=8,
            qty_precision=8,
        )

    @staticmethod
    def _is_valid_rules(rules: SymbolRules) -> bool:
        return rules.tick_size > 0 and rules.lot_size > 0 and rules.min_notional_try > 0

    def _extract_rules(self, pair: object) -> SymbolRules:
        tick_size = _to_decimal(_read_field(pair, "tick_size", "tickSize"))
        lot_size = _to_decimal(_read_field(pair, "step_size", "stepSize"))
        min_notional_try = _to_decimal(_read_field(pair, "min_total_amount", "minTotalAmount"))
        min_qty = _to_decimal(_read_field(pair, "min_quantity", "minQuantity", "minQty"))
        max_qty = _to_decimal(_read_field(pair, "max_quantity", "maxQuantity", "maxQty"))

        filters = _read_field(pair, "filters")
        if isinstance(filters, list):
            for raw_filter in filters:
                if not isinstance(raw_filter, Mapping):
                    continue
                filter_type = str(raw_filter.get("filterType") or "").upper()
                if filter_type == "PRICE_FILTER":
                    tick_size = tick_size or _to_decimal(raw_filter.get("tickSize"))
                    min_notional_try = min_notional_try or _to_decimal(
                        raw_filter.get("minExchangeValue")
                    )
                if filter_type in {"LOT_SIZE", "MARKET_LOT_SIZE", "QUANTITY_FILTER"}:
                    lot_size = lot_size or _to_decimal(raw_filter.get("stepSize"))
                    min_qty = (
                        min_qty if min_qty is not None else _to_decimal(raw_filter.get("minQty"))
                    )
                    max_qty = (
                        max_qty if max_qty is not None else _to_decimal(raw_filter.get("maxQty"))
                    )

        numerator_scale = _read_field(pair, "numerator_scale", "numeratorScale")
        denominator_scale = _read_field(pair, "denominator_scale", "denominatorScale")
        if lot_size in {None, Decimal("0")} and isinstance(numerator_scale, int):
            lot_size = Decimal("1").scaleb(-numerator_scale)

        price_precision = (
            int(denominator_scale)
            if isinstance(denominator_scale, int)
            else _infer_precision(tick_size)
        )
        qty_precision = (
            int(numerator_scale) if isinstance(numerator_scale, int) else _infer_precision(lot_size)
        )

        return SymbolRules(
            tick_size=tick_size or Decimal("0"),
            lot_size=lot_size or Decimal("0"),
            min_notional_try=min_notional_try or Decimal("0"),
            min_qty=min_qty,
            max_qty=max_qty,
            price_precision=price_precision,
            qty_precision=qty_precision,
        )

    def get_symbol_rules_status(self, symbol: str) -> tuple[SymbolRules | None, str]:
        key = _norm_symbol(symbol)
        now = datetime.now(UTC)
        cached = self._cache.get(key)
        if cached and (now - cached.cached_at) < timedelta(seconds=self.cache_ttl_sec):
            return cached.rules, cached.status

        index: dict[str, object] = {}
        get_info = getattr(self.exchange, "get_exchange_info", None)
        if callable(get_info):
            pairs = get_info()
            for pair in pairs:
                for candidate in _pair_symbol_candidates(pair):
                    index[_norm_symbol(candidate)] = pair

        match = index.get(key)
        if match is None:
            require_metadata = bool(getattr(self.settings, "stage7_rules_require_metadata", True))
            if require_metadata:
                return None, "missing"
            fallback = self._fallback_rules()
            self._cache[key] = _CachedRules(
                rules=fallback,
                status="fallback",
                cached_at=now,
            )
            return fallback, "fallback"

        converted = self._extract_rules(match)
        if not self._is_valid_rules(converted):
            require_metadata = bool(getattr(self.settings, "stage7_rules_require_metadata", True))
            if require_metadata:
                return None, "invalid"
            fallback = self._fallback_rules()
            self._cache[key] = _CachedRules(
                rules=fallback,
                status="fallback",
                cached_at=now,
            )
            return fallback, "fallback"

        cached_rules = _CachedRules(
            rules=converted,
            status="ok",
            cached_at=now,
        )
        for alias in _pair_symbol_candidates(match):
            self._cache[_norm_symbol(alias)] = cached_rules
        self._cache[key] = cached_rules
        return converted, "ok"

    def get_symbol_rules_or_none(self, symbol: str) -> SymbolRules | None:
        rules, _ = self.get_symbol_rules_status(symbol)
        return rules

    def get_rules(self, symbol: str) -> SymbolRules:
        rules, status = self.get_symbol_rules_status(symbol)
        if rules is None:
            raise ValueError(f"No usable exchange rules for symbol={symbol} status={status}")
        return rules

    def quantize_price(self, symbol: str, price: Decimal) -> Decimal:
        rules = self.get_rules(symbol)
        if price <= 0:
            raise ValueError("price must be > 0")
        if rules.tick_size > 0:
            steps = (price / rules.tick_size).to_integral_value(rounding=ROUND_DOWN)
            return steps * rules.tick_size
        quantum = Decimal("1").scaleb(-rules.price_precision)
        return price.quantize(quantum, rounding=ROUND_DOWN)

    def quantize_qty(self, symbol: str, qty: Decimal) -> Decimal:
        rules = self.get_rules(symbol)
        if qty <= 0:
            raise ValueError("qty must be > 0")
        if rules.lot_size > 0:
            steps = (qty / rules.lot_size).to_integral_value(rounding=ROUND_DOWN)
            return steps * rules.lot_size
        quantum = Decimal("1").scaleb(-rules.qty_precision)
        return qty.quantize(quantum, rounding=ROUND_DOWN)

    def validate_notional(self, symbol: str, price: Decimal, qty: Decimal) -> tuple[bool, str]:
        rules = self.get_rules(symbol)
        if price <= 0:
            return False, "price_non_positive"
        if qty <= 0:
            return False, "qty_non_positive"
        if rules.min_qty is not None and qty < rules.min_qty:
            return False, "min_qty"
        if rules.max_qty is not None and qty > rules.max_qty:
            return False, "max_qty"
        notional = price * qty
        if notional < rules.min_notional_try:
            return False, "min_notional"
        return True, "ok"

    def validate_min_notional(self, symbol: str, price: Decimal, qty: Decimal) -> bool:
        valid, _ = self.validate_notional(symbol, price, qty)
        return valid

    def get_rules_stage4(self, symbol: str) -> ExchangeRules:
        rules = self.get_rules(symbol)
        return ExchangeRules(
            tick_size=rules.tick_size,
            step_size=rules.lot_size,
            min_notional_try=rules.min_notional_try,
            price_precision=rules.price_precision,
            qty_precision=rules.qty_precision,
        )
