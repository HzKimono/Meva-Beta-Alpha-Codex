from __future__ import annotations

import ast
import hashlib
from datetime import datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import normalize_symbol
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.portfolio_policy_models import PortfolioPlan, RebalanceAction
from btcbot.domain.risk_budget import Mode
from btcbot.services.exchange_rules_service import ExchangeRulesService


class OrderBuilderService:
    def build_intents(
        self,
        *,
        cycle_id: str,
        plan: PortfolioPlan,
        mark_prices_try: dict[str, Decimal],
        rules: ExchangeRulesService,
        settings: Settings,
        final_mode: Mode,
        now_utc: datetime,
        rules_unavailable: dict[str, str] | None = None,
    ) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        if final_mode == Mode.OBSERVE_ONLY:
            return intents

        offset_bps = Decimal(str(settings.stage7_order_offset_bps))
        unavailable = rules_unavailable or {}
        buy_cycle_cap_remaining = Decimal(str(settings.notional_cap_try_per_cycle))

        ordered_actions = sorted(
            plan.actions,
            key=lambda action: (0 if action.side == "SELL" else 1, normalize_symbol(action.symbol)),
        )
        for action in ordered_actions:
            if final_mode == Mode.REDUCE_RISK_ONLY and action.side == "BUY":
                continue
            symbol = normalize_symbol(action.symbol)
            if (
                settings.spot_sell_requires_inventory
                and action.side == "SELL"
                and Decimal(str(action.est_qty)) <= 0
            ):
                intents.append(
                    self._skipped(
                        cycle_id=cycle_id,
                        symbol=symbol,
                        side=action.side,
                        reason=action.reason,
                        skip_reason="spot_sell_requires_inventory",
                        now_utc=now_utc,
                    )
                )
                continue
            if symbol in unavailable:
                intents.append(
                    self._skipped(
                        cycle_id=cycle_id,
                        symbol=symbol,
                        side=action.side,
                        reason=action.reason,
                        skip_reason=f"rules_unavailable:{unavailable[symbol]}",
                        now_utc=now_utc,
                    )
                )
                continue
            intent = self._build_action_intent(
                cycle_id=cycle_id,
                action=action,
                mark_prices_try=mark_prices_try,
                rules=rules,
                settings=settings,
                plan=plan,
                offset_bps=offset_bps,
                buy_cycle_cap_remaining=buy_cycle_cap_remaining,
                now_utc=now_utc,
            )
            intents.append(intent)
            if intent.side == "BUY" and not intent.skipped and buy_cycle_cap_remaining > Decimal("0"):
                buy_cycle_cap_remaining = max(
                    Decimal("0"), buy_cycle_cap_remaining - intent.notional_try
                )

        return intents

    def _build_action_intent(
        self,
        *,
        cycle_id: str,
        action: RebalanceAction,
        mark_prices_try: dict[str, Decimal],
        rules: ExchangeRulesService,
        settings: Settings,
        plan: PortfolioPlan,
        offset_bps: Decimal,
        buy_cycle_cap_remaining: Decimal,
        now_utc: datetime,
    ) -> OrderIntent:
        symbol = normalize_symbol(action.symbol)
        side = action.side
        mark = mark_prices_try.get(symbol)
        if mark is None or mark <= 0:
            return self._skipped(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                reason=action.reason,
                skip_reason="missing_mark_price",
                now_utc=now_utc,
            )

        decision = rules.resolve_boundary(symbol)
        if decision.rules is None:
            return self._skipped(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                reason=action.reason,
                skip_reason=f"rules_unavailable:{decision.resolution.status}",
                now_utc=now_utc,
            )

        offset_multiplier = Decimal("1") + (offset_bps / Decimal("10000"))
        if side == "BUY":
            offset_multiplier = Decimal("1") - (offset_bps / Decimal("10000"))

        price_raw = Decimal(str(mark)) * offset_multiplier
        price_try = rules.quantize_price(symbol, price_raw)
        if price_try <= 0:
            return self._skipped(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                reason=action.reason,
                skip_reason="price_rounds_to_zero",
                now_utc=now_utc,
            )

        target_notional = Decimal(str(action.target_notional_try))
        max_spend_try = target_notional
        if side == "BUY":
            max_spend_try = self._max_spend_after_buffers(
                action=action,
                decision=decision,
                plan=plan,
                settings=settings,
                price_try=price_try,
                buy_cycle_cap_remaining=buy_cycle_cap_remaining,
            )
            internal_min_notional = Decimal(str(settings.min_order_notional_try))
            if max_spend_try < max(decision.rules.min_notional_try, internal_min_notional):
                return self._skipped(
                    cycle_id=cycle_id,
                    symbol=symbol,
                    side=side,
                    reason=action.reason,
                    skip_reason="insufficient_notional_after_buffers",
                    now_utc=now_utc,
                )

        qty_raw = max_spend_try / price_try
        qty = rules.quantize_qty(symbol, qty_raw) if qty_raw > 0 else Decimal("0")
        if qty <= 0:
            return self._skipped(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                reason=action.reason,
                skip_reason="qty_rounds_to_zero",
                now_utc=now_utc,
            )

        if side == "BUY" and decision.rules.min_qty is not None and qty < decision.rules.min_qty:
            return self._skipped(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                reason=action.reason,
                skip_reason="qty_below_min_qty_after_quantize",
                now_utc=now_utc,
            )

        notional_try = price_try * qty
        if side == "BUY" and notional_try < decision.rules.min_notional_try:
            return self._skipped(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                reason=action.reason,
                skip_reason="notional_below_min_total_after_quantize",
                now_utc=now_utc,
            )

        valid, reason = rules.validate_notional(symbol, price_try, qty)
        if not valid:
            return self._skipped(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                reason=action.reason,
                skip_reason=reason,
                now_utc=now_utc,
            )

        client_order_id = self._client_order_id(
            cycle_id=cycle_id,
            symbol=symbol,
            side=side,
            price_try=price_try,
            qty=qty,
            reason=action.reason,
        )
        return OrderIntent(
            cycle_id=cycle_id,
            symbol=symbol,
            side=side,
            order_type="LIMIT",
            price_try=price_try,
            qty=qty,
            notional_try=notional_try,
            client_order_id=client_order_id,
            reason=action.reason,
            constraints_applied={
                "offset_bps": str(offset_bps),
                "quantized": "true",
                "created_at": now_utc.isoformat(),
            },
            skipped=False,
            skip_reason=None,
        )

    def _max_spend_after_buffers(
        self,
        *,
        action: RebalanceAction,
        decision: ExchangeRulesService.RulesBoundaryDecision,
        plan: PortfolioPlan,
        settings: Settings,
        price_try: Decimal,
        buy_cycle_cap_remaining: Decimal,
    ) -> Decimal:
        investable_try = Decimal(str(action.target_notional_try))
        max_per_order = Decimal(str(settings.max_notional_per_order_try))
        try_cash_available = self._plan_cash_available(plan)
        upper_limits = [investable_try, try_cash_available]
        if buy_cycle_cap_remaining > Decimal("0"):
            upper_limits.append(buy_cycle_cap_remaining)
        if max_per_order > Decimal("0"):
            upper_limits.append(max_per_order)
        max_spend_try = min(upper_limits)

        fee_buffer_ratio = self._resolve_fee_buffer_ratio(settings)
        spend_after_fee = max_spend_try * (Decimal("1") - fee_buffer_ratio)
        rounding_buffer_try = self._resolve_rounding_buffer_try(
            settings=settings,
            decision=decision,
            price_try=price_try,
        )
        return max(Decimal("0"), spend_after_fee - rounding_buffer_try)

    @staticmethod
    def _resolve_fee_buffer_ratio(settings: Settings) -> Decimal:
        ratio = Decimal(str(settings.fee_buffer_ratio))
        if ratio > Decimal("0"):
            return min(ratio, Decimal("1"))
        bps_ratio = Decimal(str(settings.allocation_fee_buffer_bps)) / Decimal("10000")
        return min(max(bps_ratio, Decimal("0")), Decimal("1"))

    @staticmethod
    def _resolve_rounding_buffer_try(
        *,
        settings: Settings,
        decision: ExchangeRulesService.RulesBoundaryDecision,
        price_try: Decimal,
    ) -> Decimal:
        configured = Decimal(str(getattr(settings, "rounding_buffer_try", Decimal("0"))))
        if configured > Decimal("0"):
            return configured
        rules = decision.rules
        assert rules is not None
        return max(price_try * rules.step_size, rules.tick_size * rules.step_size)

    @staticmethod
    def _plan_cash_available(plan: PortfolioPlan) -> Decimal:
        raw_snapshot = plan.constraints_summary.get("snapshot")
        if isinstance(raw_snapshot, str) and raw_snapshot:
            try:
                parsed = ast.literal_eval(raw_snapshot)
            except (ValueError, SyntaxError):
                return Decimal("Infinity")
            if isinstance(parsed, dict) and "cash_try" in parsed:
                return Decimal(str(parsed["cash_try"]))
        return Decimal("Infinity")

    def _skipped(
        self,
        *,
        cycle_id: str,
        symbol: str,
        side: str,
        reason: str,
        skip_reason: str,
        now_utc: datetime,
    ) -> OrderIntent:
        return OrderIntent(
            cycle_id=cycle_id,
            symbol=symbol,
            side=side,
            order_type="LIMIT",
            price_try=Decimal("0"),
            qty=Decimal("0"),
            notional_try=Decimal("0"),
            client_order_id=self._client_order_id(
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                price_try=Decimal("0"),
                qty=Decimal("0"),
                reason=reason,
            ),
            reason=reason,
            constraints_applied={"skipped": "true", "created_at": now_utc.isoformat()},
            skipped=True,
            skip_reason=skip_reason,
        )

    def _client_order_id(
        self,
        *,
        cycle_id: str,
        symbol: str,
        side: str,
        price_try: Decimal,
        qty: Decimal,
        reason: str,
    ) -> str:
        payload = "|".join(
            [
                cycle_id,
                symbol,
                side,
                format(price_try, "f"),
                format(qty, "f"),
                reason,
            ]
        )
        short_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"s7:{cycle_id}:{symbol}:{side}:{short_hash}"
