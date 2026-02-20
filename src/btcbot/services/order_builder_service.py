from __future__ import annotations

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
                offset_bps=offset_bps,
                now_utc=now_utc,
            )
            intents.append(intent)

        return intents

    def _build_action_intent(
        self,
        *,
        cycle_id: str,
        action: RebalanceAction,
        mark_prices_try: dict[str, Decimal],
        rules: ExchangeRulesService,
        offset_bps: Decimal,
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
        qty_raw = target_notional / price_try
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

        valid, reason = rules.validate_notional(symbol, price_try, qty)
        notional_try = price_try * qty
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
