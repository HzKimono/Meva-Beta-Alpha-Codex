from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import Balance, normalize_symbol
from btcbot.domain.portfolio_policy_models import PortfolioPlan
from btcbot.domain.risk_models import ExposureSnapshot, stable_hash_payload


class ExposureTracker:
    def compute_snapshot(
        self,
        *,
        balances: list[Balance],
        mark_prices_try: dict[str, Decimal],
        settings: Settings,
        now_utc: datetime,
        plan: PortfolioPlan | None = None,
    ) -> ExposureSnapshot:
        quote_ccy = settings.stage7_universe_quote_ccy.upper()
        per_symbol: dict[str, Decimal] = {}
        free_cash_try = Decimal("0")

        for balance in sorted(balances, key=lambda item: item.asset.upper()):
            qty = Decimal(str(balance.free)) + Decimal(str(balance.locked))
            asset = balance.asset.upper()
            if asset == quote_ccy:
                free_cash_try += Decimal(str(balance.free))
                continue
            symbol = normalize_symbol(f"{asset}{quote_ccy}")
            price = mark_prices_try.get(symbol)
            if price is None:
                continue
            per_symbol[symbol] = per_symbol.get(symbol, Decimal("0")) + (qty * price)

        sorted_exposure = dict(sorted(per_symbol.items(), key=lambda item: item[0]))
        total_exposure_try = sum(sorted_exposure.values(), Decimal("0"))

        top_n = max(1, settings.stage7_concentration_top_n)
        concentration = sorted(
            sorted_exposure.items(), key=lambda item: (-item[1], item[0])
        )[:top_n]

        turnover_estimate = Decimal("0")
        if plan is not None:
            turnover_estimate = sum(
                (action.target_notional_try.copy_abs() for action in plan.actions), Decimal("0")
            )

        inputs_hash = stable_hash_payload(
            {
                "balances": [
                    {
                        "asset": balance.asset.upper(),
                        "free": str(balance.free),
                        "locked": str(balance.locked),
                    }
                    for balance in sorted(balances, key=lambda item: item.asset.upper())
                ],
                "mark_prices_try": {k: str(v) for k, v in sorted(mark_prices_try.items())},
                "top_n": top_n,
            }
        )

        return ExposureSnapshot(
            per_symbol_exposure_try=sorted_exposure,
            total_exposure_try=total_exposure_try,
            concentration_top_n=concentration,
            turnover_estimate_try=turnover_estimate,
            free_cash_try=free_cash_try,
            computed_at=now_utc.astimezone(UTC),
            inputs_hash=inputs_hash,
        )

