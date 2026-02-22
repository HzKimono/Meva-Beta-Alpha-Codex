from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from btcbot.adapters.exchange import ExchangeClient
from btcbot.config import Settings
from btcbot.domain.money_policy import policy_for_symbol, round_price, round_qty
from btcbot.domain.stage4 import ExchangeRules

logger = logging.getLogger(__name__)


def _norm_symbol(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def _pair_symbol_candidates(pair: object) -> list[str]:
    if isinstance(pair, Mapping):
        candidates = [
            pair.get("pairSymbol"),
            pair.get("pairSymbolNormalized"),
            pair.get("symbol"),
            pair.get("name"),
            pair.get("nameNormalized"),
            pair.get("name_normalized"),
        ]
        return [candidate for candidate in candidates if isinstance(candidate, str) and candidate]

    candidates = [
        getattr(pair, "pair_symbol", None),
        getattr(pair, "pair_symbol_normalized", None),
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


def _to_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(str(value))
    except Exception:  # noqa: BLE001
        return None


def _positive_decimal_values(values: list[object]) -> list[Decimal]:
    parsed: list[Decimal] = []
    for value in values:
        dec = _to_decimal(value)
        if dec is not None and dec > 0:
            parsed.append(dec)
    return parsed


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


@dataclass(frozen=True)
class SymbolRulesResolution:
    symbol: str
    status: Literal[
        "ok",
        "fallback",
        "missing",
        "invalid_metadata",
        "unsupported_schema_variant",
        "upstream_fetch_failure",
    ]
    rules: SymbolRules | None
    reason: str | None = None
    details: dict[str, object] | None = None

    @property
    def usable(self) -> bool:
        return self.rules is not None


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

    @staticmethod
    def _serialize_issues(*, missing: list[str], invalid: list[str], source: str) -> str:
        parts = [f"source={source}"]
        if missing:
            parts.append(f"missing={','.join(sorted(set(missing)))}")
        if invalid:
            parts.append(f"invalid={','.join(sorted(set(invalid)))}")
        return ";".join(parts)

    @staticmethod
    def _status_to_boundary(
        status: str,
        *,
        require_metadata: bool,
    ) -> tuple[str, str]:
        if status == "ok":
            return "OK", "ok"
        if status == "fallback":
            return "DEGRADE", "fallback_rules"
        if status in {"missing", "invalid_metadata", "unsupported_schema_variant"}:
            return "SKIP", status if require_metadata else "fallback_blocked"
        return "SKIP", "upstream_fetch_failure"

    @dataclass(frozen=True)
    class RulesBoundaryDecision:
        symbol: str
        outcome: Literal["OK", "SKIP", "DEGRADE"]
        rules: SymbolRules | None
        reason: str
        resolution: SymbolRulesResolution

    def _collect_min_notional_candidates(self, pair: object) -> list[Decimal]:
        candidate_fields = [
            "min_total_amount",
            "minTotalAmount",
            "minExchangeValue",
            "minTotal",
            "minNotional",
            "minNotionalValue",
            "minOrderAmount",
            "minimumOrderAmount",
            "minQuoteAmount",
            "min_trade_amount",
        ]
        raw_values = [_read_field(pair, field) for field in candidate_fields]

        filters = _read_field(pair, "filters")
        if isinstance(filters, Mapping):
            filters = list(filters.values())
        if isinstance(filters, list):
            for raw_filter in filters:
                if not isinstance(raw_filter, Mapping):
                    continue
                for field in (
                    "minTotalAmount",
                    "minExchangeValue",
                    "minAmount",
                    "minNotional",
                    "minNotionalValue",
                    "minOrderAmount",
                    "minQuoteAmount",
                ):
                    raw_values.append(raw_filter.get(field))

        constraints = _read_field(pair, "constraints", "ruleSet", "tradingRules")
        if isinstance(constraints, Mapping):
            for field in (
                "minTotalAmount",
                "minExchangeValue",
                "minNotional",
                "minNotionalValue",
                "minOrderAmount",
                "minQuoteAmount",
            ):
                raw_values.append(constraints.get(field))

        return _positive_decimal_values(raw_values)

    def _is_try_quote_pair(self, pair: object) -> bool:
        quote = _read_field(pair, "denominator", "quoteAsset", "quote")
        if isinstance(quote, str) and quote.strip().upper() == "TRY":
            return True
        for candidate in _pair_symbol_candidates(pair):
            normalized = _norm_symbol(candidate)
            if normalized.endswith("TRY"):
                return True
        return False

    def _derive_safe_min_notional_try(self) -> Decimal:
        configured = Decimal(
            str(getattr(self.settings, "stage7_rules_safe_min_notional_try", Decimal("100")))
        )
        fallback = Decimal(
            str(getattr(self.settings, "stage7_rules_fallback_min_notional_try", Decimal("10")))
        )
        minimum_order = Decimal(str(getattr(self.settings, "min_order_notional_try", 0)))
        return max(configured, fallback, minimum_order, Decimal("100"))

    def _extract_rules(self, pair: object) -> tuple[SymbolRules | None, str, dict[str, object]]:
        source = type(pair).__name__
        missing_fields: list[str] = []
        invalid_fields: list[str] = []

        tick_size = _to_decimal(_read_field(pair, "tick_size", "tickSize"))
        lot_size = _to_decimal(_read_field(pair, "step_size", "stepSize", "lotSize"))
        min_notional_candidates = self._collect_min_notional_candidates(pair)
        min_notional_try = max(min_notional_candidates) if min_notional_candidates else None
        min_qty = _to_decimal(_read_field(pair, "min_quantity", "minQuantity", "minQty"))
        max_qty = _to_decimal(_read_field(pair, "max_quantity", "maxQuantity", "maxQty"))

        filters = _read_field(pair, "filters")
        if isinstance(filters, Mapping):
            filters = list(filters.values())
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
                    min_notional_try = min_notional_try or _to_decimal(raw_filter.get("minAmount"))
                    min_notional_try = min_notional_try or _to_decimal(
                        raw_filter.get("minNotional")
                    )
                if filter_type in {"LOT_SIZE", "MARKET_LOT_SIZE", "QUANTITY_FILTER"}:
                    lot_size = lot_size or _to_decimal(raw_filter.get("stepSize"))
                    min_qty = (
                        min_qty if min_qty is not None else _to_decimal(raw_filter.get("minQty"))
                    )
                    max_qty = (
                        max_qty if max_qty is not None else _to_decimal(raw_filter.get("maxQty"))
                    )
                if filter_type in {"MIN_TOTAL", "MIN_NOTIONAL", "NOTIONAL"}:
                    min_notional_try = min_notional_try or _to_decimal(
                        raw_filter.get("minTotalAmount")
                    )
                    min_notional_try = min_notional_try or _to_decimal(
                        raw_filter.get("minExchangeValue")
                    )
                    min_notional_try = min_notional_try or _to_decimal(raw_filter.get("minAmount"))
                    min_notional_try = min_notional_try or _to_decimal(
                        raw_filter.get("minNotional")
                    )
        elif filters is not None:
            return (
                None,
                "unsupported_schema_variant",
                {
                    "source": source,
                    "reason": "filters_not_list",
                },
            )

        constraints = _read_field(pair, "constraints", "ruleSet", "tradingRules")
        if isinstance(constraints, Mapping):
            tick_size = tick_size or _to_decimal(
                constraints.get("tickSize") or constraints.get("tick_size")
            )
            lot_size = lot_size or _to_decimal(
                constraints.get("stepSize") or constraints.get("step_size")
            )
            min_notional_try = min_notional_try or _to_decimal(
                constraints.get("minNotional")
                or constraints.get("min_total_amount")
                or constraints.get("minExchangeValue")
            )
            min_qty = min_qty if min_qty is not None else _to_decimal(constraints.get("minQty"))
            max_qty = max_qty if max_qty is not None else _to_decimal(constraints.get("maxQty"))

        numerator_scale = _to_int(_read_field(pair, "numerator_scale", "numeratorScale"))
        denominator_scale = _to_int(_read_field(pair, "denominator_scale", "denominatorScale"))
        if lot_size in {None, Decimal("0")} and min_qty and min_qty > 0:
            lot_size = min_qty
        if lot_size in {None, Decimal("0")} and numerator_scale is not None:
            lot_size = Decimal("1").scaleb(-numerator_scale)

        price_precision = (
            denominator_scale if denominator_scale is not None else _infer_precision(tick_size)
        )
        qty_precision = (
            numerator_scale if numerator_scale is not None else _infer_precision(lot_size)
        )

        min_notional_source = "metadata"
        if min_notional_try is None and self._is_try_quote_pair(pair):
            min_notional_try = self._derive_safe_min_notional_try()
            min_notional_source = "safe_default_try_quote"

        rules = SymbolRules(
            tick_size=tick_size or Decimal("0"),
            lot_size=lot_size or Decimal("0"),
            min_notional_try=min_notional_try or Decimal("0"),
            min_qty=min_qty,
            max_qty=max_qty,
            price_precision=price_precision,
            qty_precision=qty_precision,
        )

        if tick_size is None:
            missing_fields.append("tick_size")
        if lot_size is None:
            missing_fields.append("lot_size")
        if min_notional_try is None:
            missing_fields.append("min_notional_try")
        if rules.tick_size <= 0:
            invalid_fields.append("tick_size")
        if rules.lot_size <= 0:
            invalid_fields.append("lot_size")
        if rules.min_notional_try <= 0:
            invalid_fields.append("min_notional_try")

        if missing_fields or invalid_fields:
            return (
                None,
                "invalid_metadata",
                {
                    "source": source,
                    "missing_fields": sorted(set(missing_fields)),
                    "invalid_fields": sorted(set(invalid_fields)),
                },
            )

        return rules, "ok", {"source": source, "min_notional_source": min_notional_source}

    def resolve_symbol_rules(self, symbol: str) -> SymbolRulesResolution:
        key = _norm_symbol(symbol)
        now = datetime.now(UTC)
        cached = self._cache.get(key)
        if cached and (now - cached.cached_at) < timedelta(seconds=self.cache_ttl_sec):
            return SymbolRulesResolution(symbol=key, rules=cached.rules, status=cached.status)

        index: dict[str, object] = {}
        get_info = getattr(self.exchange, "get_exchange_info", None)
        if callable(get_info):
            try:
                pairs = get_info()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "exchange_rules_exchange_info_error", extra={"extra": {"symbol": key}}
                )
                return SymbolRulesResolution(
                    symbol=key,
                    rules=None,
                    status="upstream_fetch_failure",
                    reason=f"upstream_fetch_failure:{type(exc).__name__}",
                    details={"error_type": type(exc).__name__},
                )
            for pair in pairs:
                for candidate in _pair_symbol_candidates(pair):
                    index[_norm_symbol(candidate)] = pair

        match = index.get(key)
        if match is None:
            require_metadata = bool(getattr(self.settings, "stage7_rules_require_metadata", True))
            if require_metadata:
                return SymbolRulesResolution(
                    symbol=key, rules=None, status="missing", reason="metadata_missing"
                )
            fallback = self._fallback_rules()
            self._cache[key] = _CachedRules(
                rules=fallback,
                status="fallback",
                cached_at=now,
            )
            return SymbolRulesResolution(symbol=key, rules=fallback, status="fallback")

        converted, parse_status, parse_details = self._extract_rules(match)
        if parse_status != "ok" or converted is None:
            require_metadata = bool(getattr(self.settings, "stage7_rules_require_metadata", True))
            if require_metadata:
                detail_reason = self._serialize_issues(
                    missing=list(parse_details.get("missing_fields") or []),
                    invalid=list(parse_details.get("invalid_fields") or []),
                    source=str(parse_details.get("source") or type(match).__name__),
                )
                return SymbolRulesResolution(
                    symbol=key,
                    rules=None,
                    status=parse_status,
                    reason=detail_reason,
                    details=parse_details,
                )
            fallback = self._fallback_rules()
            self._cache[key] = _CachedRules(
                rules=fallback,
                status="fallback",
                cached_at=now,
            )
            return SymbolRulesResolution(symbol=key, rules=fallback, status="fallback")

        cached_rules = _CachedRules(
            rules=converted,
            status="ok",
            cached_at=now,
        )
        for alias in _pair_symbol_candidates(match):
            self._cache[_norm_symbol(alias)] = cached_rules
        self._cache[key] = cached_rules
        return SymbolRulesResolution(symbol=key, rules=converted, status="ok")

    def get_symbol_rules_status(self, symbol: str) -> tuple[SymbolRules | None, str]:
        result = self.resolve_symbol_rules(symbol)
        status = result.status if result.status != "upstream_fetch_failure" else "missing"
        return result.rules, status

    def get_symbol_rules_or_none(self, symbol: str) -> SymbolRules | None:
        rules, _ = self.get_symbol_rules_status(symbol)
        return rules

    def get_rules(self, symbol: str) -> SymbolRules:
        resolution = self.resolve_symbol_rules(symbol)
        if resolution.rules is None:
            reason = f" reason={resolution.reason}" if resolution.reason else ""
            raise ValueError(
                f"No usable exchange rules for symbol={symbol} status={resolution.status}{reason}"
            )
        return resolution.rules

    def resolve_boundary(self, symbol: str) -> RulesBoundaryDecision:
        normalized = _norm_symbol(symbol)
        resolution = self.resolve_symbol_rules(normalized)
        require_metadata = bool(getattr(self.settings, "stage7_rules_require_metadata", True))
        outcome, reason = self._status_to_boundary(
            resolution.status,
            require_metadata=require_metadata,
        )
        return self.RulesBoundaryDecision(
            symbol=normalized,
            outcome=outcome,
            rules=resolution.rules,
            reason=reason if resolution.reason is None else resolution.reason,
            resolution=resolution,
        )

    def quantize_price(self, symbol: str, price: Decimal) -> Decimal:
        rules = self.get_rules(symbol)
        if price <= 0:
            raise ValueError("price must be > 0")
        return round_price(price, policy_for_symbol(rules))

    def quantize_qty(self, symbol: str, qty: Decimal) -> Decimal:
        rules = self.get_rules(symbol)
        if qty <= 0:
            raise ValueError("qty must be > 0")
        return round_qty(qty, policy_for_symbol(rules))

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
