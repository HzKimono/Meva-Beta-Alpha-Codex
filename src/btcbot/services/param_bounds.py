from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.adaptation_models import Stage7Params

_DECIMAL_ZERO = Decimal("0")
_DECIMAL_ONE = Decimal("1")
_KEYS = ("liquidity", "spread", "volatility")


class ParamBounds:
    @staticmethod
    def normalize_weights(weights: dict[str, Decimal]) -> dict[str, Decimal]:
        merged = {key: Decimal(str(weights.get(key, _DECIMAL_ZERO))) for key in _KEYS}
        clipped = {
            key: min(_DECIMAL_ONE, max(_DECIMAL_ZERO, value)) for key, value in merged.items()
        }
        total = sum(clipped.values(), _DECIMAL_ZERO)
        if total <= _DECIMAL_ZERO:
            return {
                "liquidity": Decimal("0.5"),
                "spread": Decimal("0.3"),
                "volatility": Decimal("0.2"),
            }
        return {key: clipped[key] / total for key in _KEYS}

    @staticmethod
    def apply_bounds(params: Stage7Params, settings: Settings) -> Stage7Params:
        turnover_upper = max(_DECIMAL_ZERO, Decimal(str(settings.notional_cap_try_per_cycle)))
        bounded = replace(
            params,
            universe_size=min(50, max(5, int(params.universe_size))),
            score_weights=ParamBounds.normalize_weights(params.score_weights),
            order_offset_bps=min(50, max(0, int(params.order_offset_bps))),
            turnover_cap_try=min(
                turnover_upper, max(_DECIMAL_ZERO, Decimal(str(params.turnover_cap_try)))
            ),
            max_orders_per_cycle=min(20, max(1, int(params.max_orders_per_cycle))),
            max_spread_bps=min(500, max(10, int(params.max_spread_bps))),
            cash_target_try=min(
                Decimal(str(settings.try_cash_max)),
                max(_DECIMAL_ZERO, Decimal(str(params.cash_target_try))),
            ),
            min_quote_volume_try=max(_DECIMAL_ZERO, Decimal(str(params.min_quote_volume_try))),
        )
        return bounded


def has_rollback_trigger(
    *,
    recent_metrics: list[dict[str, object]],
    throttled_persist_cycles: int = 3,
    pnl_floor_try: Decimal = Decimal("-50"),
    pnl_floor_cycles: int = 3,
) -> bool:
    if not recent_metrics:
        return False
    ordered = list(recent_metrics)
    if any(bool(dict(item.get("alert_flags") or {}).get("drawdown_breach")) for item in ordered):
        return True
    if any(bool(dict(item.get("alert_flags") or {}).get("reject_spike")) for item in ordered):
        return True
    if len(ordered) >= throttled_persist_cycles and all(
        bool(dict(item.get("alert_flags") or {}).get("throttled"))
        for item in ordered[:throttled_persist_cycles]
    ):
        return True
    if len(ordered) >= pnl_floor_cycles:
        values = [Decimal(str(item.get("net_pnl_try", "0"))) for item in ordered[:pnl_floor_cycles]]
        if all(value < pnl_floor_try for value in values):
            return True
    return False
