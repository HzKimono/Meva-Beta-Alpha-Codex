from __future__ import annotations

from decimal import Decimal

from btcbot.domain.strategy_core import Intent, StrategyContext


class BaselineMeanReversionStrategy:
    id = "baseline_mean_reversion_v1"

    def generate_intents(self, context: StrategyContext) -> list[Intent]:
        threshold_bps = Decimal(context.knobs.mean_reversion_bps)
        anchor = (
            context.mark_price
            if context.knobs.anchor_price is None
            else Decimal(context.knobs.anchor_price)
        )
        if anchor <= Decimal("0"):
            return []

        mark_price = Decimal(context.mark_price)
        deviation_bps = ((mark_price - anchor) / anchor) * Decimal("10000")
        try_balance = Decimal(context.balances.get("TRY", Decimal("0")))
        max_notional = min(Decimal(context.knobs.max_notional_try), try_balance)
        if max_notional <= Decimal("0"):
            return []

        if context.position is None or context.position.qty <= Decimal("0"):
            if context.open_orders.buy_count > 0:
                return []
            bootstrap = min(Decimal(context.knobs.bootstrap_notional_try), max_notional)
            if bootstrap <= Decimal("0"):
                return []
            return [
                Intent(
                    symbol=context.symbol,
                    side="buy",
                    intent_type="place",
                    target_notional_try=bootstrap,
                    rationale="bootstrap_position",
                    strategy_id=self.id,
                    confidence=Decimal("0.60"),
                )
            ]

        if deviation_bps <= -threshold_bps:
            return [
                Intent(
                    symbol=context.symbol,
                    side="buy",
                    intent_type="place",
                    target_notional_try=max_notional,
                    rationale="mean_reversion_buy",
                    strategy_id=self.id,
                    confidence=Decimal("0.70"),
                )
            ]

        if deviation_bps >= threshold_bps:
            if context.open_orders.sell_count > 0:
                return []
            position_value = context.position.qty * mark_price
            max_sell_notional = min(max_notional, position_value)
            if max_sell_notional <= Decimal("0"):
                return []
            return [
                Intent(
                    symbol=context.symbol,
                    side="sell",
                    intent_type="place",
                    target_notional_try=max_sell_notional,
                    rationale="mean_reversion_sell",
                    strategy_id=self.id,
                    confidence=Decimal("0.70"),
                )
            ]

        return []
