from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import Balance
from btcbot.domain.risk_budget import Mode
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.order_builder_service import OrderBuilderService
from btcbot.services.portfolio_policy_service import PortfolioPolicyService
from btcbot.services.stage7_cycle_runner import Stage7CycleRunner
from btcbot.services.stage7_planning_kernel_integration import Stage7OrderIntentBuilderAdapter
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


def _base_kwargs(use_kernel: bool) -> dict[str, object]:
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
        "open_orders": [],
        "final_mode": Mode.NORMAL,
        "rules_service": ExchangeRulesService(_Exchange()),
        "rules_unavailable": {},
        "selected_universe": ["BTCTRY", "ETHTRY"],
        "policy_service": PortfolioPolicyService(),
        "order_builder": OrderBuilderService(),
    }


def _canonical(intents: list[object]) -> list[tuple[str, str, str, str, str, bool, str | None]]:
    normalized = []
    for item in intents:
        normalized.append(
            (
                item.symbol,
                item.side,
                item.order_type,
                str(item.price_try),
                str(item.qty),
                bool(item.skipped),
                item.skip_reason,
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


def test_side_and_order_type_normalization() -> None:
    assert Stage7OrderIntentBuilderAdapter.normalize_side("buy") == "BUY"
    assert Stage7OrderIntentBuilderAdapter.normalize_side("SELL") == "SELL"
    assert Stage7OrderIntentBuilderAdapter.normalize_side("unknown") == "BUY"

    assert Stage7OrderIntentBuilderAdapter.normalize_order_type("limit") == "LIMIT"
    assert Stage7OrderIntentBuilderAdapter.normalize_order_type("MARKET") == "MARKET"
    assert Stage7OrderIntentBuilderAdapter.normalize_order_type("other") == "LIMIT"
