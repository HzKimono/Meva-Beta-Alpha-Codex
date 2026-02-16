from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import Balance
from btcbot.domain.order_intent import OrderIntent
from btcbot.domain.risk_budget import Mode
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.order_builder_service import OrderBuilderService
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.stage7_cycle_runner import Stage7CycleRunner
from btcbot.services.stage7_planning_kernel_integration import (
    Stage7OrderIntentBuilderAdapter,
    normalize_stage4_open_orders,
)
from btcbot.services.universe_selection_service import UniverseSelectionService


class _Pair:
    def __init__(self, symbol: str) -> None:
        self.pair_symbol = symbol
        self.name = symbol.replace("_", "")
        self.nameNormalized = symbol
        self.numeratorScale = 6
        self.denominatorScale = 2
        self.minTotalAmount = Decimal("100")
        self.tickSize = Decimal("0.1")
        self.stepSize = Decimal("0.01")
        self.minQuantity = Decimal("0.01")
        self.maxQuantity = None


class _Exchange:
    def get_exchange_info(self):
        return [_Pair("BTC_TRY"), _Pair("ETH_TRY")]


class _OpenOrder:
    def __init__(self, *, symbol: str, side: str, client_order_id: str) -> None:
        self.symbol = symbol
        self.side = side
        self.type = "limit"
        self.price = Decimal("100")
        self.qty = Decimal("0.10")
        self.client_order_id = client_order_id
        self.status = "simulated_submitted"


def _base_kwargs(use_kernel: bool, open_orders: list[object] | None = None) -> dict[str, object]:
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_USE_PLANNING_KERNEL=use_kernel)
    return {
        "cycle_id": "c1",
        "now": datetime(2024, 1, 1, tzinfo=UTC),
        "runtime": settings,
        "universe_service": UniverseSelectionService(),
        "base_client": _Exchange(),
        "mark_prices": {"BTCTRY": Decimal("1000"), "ETHTRY": Decimal("100")},
        "balances": [
            Balance(asset="TRY", free=Decimal("10000")),
            Balance(asset="BTC", free=Decimal("0.10")),
            Balance(asset="ETH", free=Decimal("1.0")),
        ],
        "open_orders": open_orders or [],
        "final_mode": Mode.NORMAL,
        "rules_service": ExchangeRulesService(_Exchange()),
        "rules_unavailable": {},
        "selected_universe": ["BTCTRY", "ETHTRY"],
        "policy_service": PortfolioPolicyService(),
        "order_builder": OrderBuilderService(),
    }


def _canonical(intents: list[OrderIntent]) -> list[tuple[str, ...]]:
    normalized = []
    for item in intents:
        normalized.append(
            (
                item.client_order_id,
                item.symbol,
                item.side,
                item.order_type,
                str(item.price_try),
                str(item.qty),
                str(item.notional_try),
                item.reason,
                str(item.skipped),
                str(item.skip_reason),
                repr(sorted(item.constraints_applied.items())),
            )
        )
    return sorted(normalized)


def test_stage7_kernel_planning_matches_legacy_order_intents_for_same_inputs() -> None:
    runner = Stage7CycleRunner()

    _, legacy, legacy_engine = runner._build_stage7_order_intents(**_base_kwargs(use_kernel=False))
    _, kernel, kernel_engine = runner._build_stage7_order_intents(**_base_kwargs(use_kernel=True))

    assert legacy_engine == "legacy"
    assert kernel_engine == "kernel"
    assert _canonical(kernel) == _canonical(legacy)


def test_stage7_kernel_planning_matches_legacy_with_non_empty_open_orders() -> None:
    runner = Stage7CycleRunner()
    open_orders = [_OpenOrder(symbol="BTC_TRY", side="BUY", client_order_id="s7-open-1")]

    _, legacy, _ = runner._build_stage7_order_intents(
        **_base_kwargs(use_kernel=False, open_orders=open_orders)
    )
    _, kernel, _ = runner._build_stage7_order_intents(
        **_base_kwargs(use_kernel=True, open_orders=open_orders)
    )

    assert _canonical(kernel) == _canonical(legacy)


def test_side_and_order_type_normalization_is_strict() -> None:
    assert Stage7OrderIntentBuilderAdapter.normalize_side("buy") == "BUY"
    assert Stage7OrderIntentBuilderAdapter.normalize_side("SELL") == "SELL"
    assert Stage7OrderIntentBuilderAdapter.normalize_side("unknown") is None

    assert Stage7OrderIntentBuilderAdapter.normalize_order_type("limit") == "LIMIT"
    assert Stage7OrderIntentBuilderAdapter.normalize_order_type("MARKET") == "MARKET"
    assert Stage7OrderIntentBuilderAdapter.normalize_order_type("other") is None


def test_invalid_side_or_order_type_is_skipped_safely() -> None:
    class _BadBuilder(OrderBuilderService):
        def build_intents(self, **kwargs):  # type: ignore[override]
            cycle_id = kwargs["cycle_id"]
            return [
                OrderIntent(
                    cycle_id=cycle_id,
                    symbol="BTCTRY",
                    side="INVALID",  # type: ignore[arg-type]
                    order_type="BROKEN",  # type: ignore[arg-type]
                    price_try=Decimal("100"),
                    qty=Decimal("1"),
                    notional_try=Decimal("100"),
                    client_order_id="bad-1",
                    reason="bad",
                    constraints_applied={},
                    skipped=False,
                    skip_reason=None,
                )
            ]

    runner = Stage7CycleRunner()
    _, kernel, _ = runner._build_stage7_order_intents(
        **{
            **_base_kwargs(use_kernel=True),
            "order_builder": _BadBuilder(),
        }
    )
    assert len(kernel) == 1
    assert kernel[0].skipped is True
    assert kernel[0].skip_reason == "invalid_normalized_fields"


def test_open_order_normalization() -> None:
    views = normalize_stage4_open_orders(
        [_OpenOrder(symbol="btc_try", side="buy", client_order_id="cid-1")]
    )
    assert len(views) == 1
    assert views[0].symbol == "BTCTRY"
    assert views[0].side == "BUY"
    assert views[0].order_type == "LIMIT"
