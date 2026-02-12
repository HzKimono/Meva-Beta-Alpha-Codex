from __future__ import annotations

from decimal import Decimal

from btcbot.domain.intent import Intent
from btcbot.domain.models import OrderSide, normalize_symbol
from btcbot.strategies.context import StrategyContext


class ProfitAwareStrategyV1:
    def generate_intents(self, context: StrategyContext) -> list[Intent]:
        intents: list[Intent] = []
        profit_factor = Decimal("1") + (Decimal(context.settings.min_profit_bps) / Decimal("10000"))

        for symbol in sorted(context.symbols):
            normalized = normalize_symbol(symbol)
            bid, ask = context.orderbooks.get(normalized, (Decimal("0"), Decimal("0")))
            position = context.positions.get(
                normalized,
            )
            if position and position.qty > 0 and bid >= position.avg_cost * profit_factor:
                sell_qty = position.qty * Decimal("0.25")
                if sell_qty > 0:
                    intents.append(
                        Intent.create(
                            cycle_id=context.cycle_id,
                            symbol=normalized,
                            side=OrderSide.SELL,
                            qty=sell_qty,
                            limit_price=bid,
                            reason="take_profit",
                            confidence=0.9,
                            ttl_seconds=context.settings.ttl_seconds,
                        )
                    )
                continue

            if position and position.qty > 0:
                continue

            spread_ok = bid > 0 and ask > 0 and (ask - bid) / bid <= Decimal("0.01")
            if not spread_ok:
                continue

            try_balance = Decimal("0")
            for bal in context.balances:
                if bal.asset.upper() == "TRY":
                    try_balance = Decimal(str(bal.free))
                    break
            budget = min(try_balance, Decimal("100"))
            if budget <= 0:
                continue
            qty = budget / ask
            intents.append(
                Intent.create(
                    cycle_id=context.cycle_id,
                    symbol=normalized,
                    side=OrderSide.BUY,
                    qty=qty,
                    limit_price=ask,
                    reason="conservative_entry",
                    confidence=0.55,
                    ttl_seconds=context.settings.ttl_seconds,
                )
            )

        return intents
