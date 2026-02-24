from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.portfolio_policy_models import PortfolioPlan, RebalanceAction, TargetAllocation
from btcbot.domain.risk_budget import Mode
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.order_builder_service import OrderBuilderService


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
        return [_Pair("BTC_TRY")]


def _plan(
    actions: list[RebalanceAction],
    balances: dict[str, dict[str, Decimal | str]] | None = None,
) -> PortfolioPlan:
    return PortfolioPlan(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        universe=["BTCTRY"],
        cash_target_try=Decimal("0"),
        allocations=[
            TargetAllocation(
                symbol="BTCTRY", weight=Decimal("1"), target_notional_try=Decimal("1000")
            )
        ],
        actions=actions,
        constraints_summary={
            "balances": balances
            or {"BTC": {"free": "10", "locked": "0"}, "TRY": {"free": "0", "locked": "0"}}
        },
        notes=[],
    )


def test_tick_lot_quantization_correctness() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"), ALLOCATION_FEE_BUFFER_BPS=Decimal("0"), ROUNDING_BUFFER_TRY=Decimal("0.00000001"))
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="BUY",
                    target_notional_try=Decimal("1000"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("101.23")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert len(intents) == 1
    expected_price = rules.quantize_price("BTCTRY", Decimal("101.23"))
    expected_qty = rules.quantize_qty("BTCTRY", (Decimal("1000") - Decimal("0.00000001")) / expected_price)
    assert intents[0].price_try == expected_price
    assert intents[0].qty == expected_qty


def test_insufficient_notional_after_buffers_skip() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="BUY",
                    target_notional_try=Decimal("10"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("101")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert intents[0].skipped is True
    assert intents[0].skip_reason == "insufficient_notional_after_buffers"


def test_deterministic_client_order_id() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    kwargs = dict(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("500"),
                    est_qty=Decimal("1"),
                    reason="trim",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("500")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    a = builder.build_intents(**kwargs)[0]
    b = builder.build_intents(**kwargs)[0]
    assert a.client_order_id == b.client_order_id


def test_sell_first_preserved() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="BUY",
                    target_notional_try=Decimal("1000"),
                    est_qty=Decimal("1"),
                    reason="buy",
                ),
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("100"),
                    est_qty=Decimal("1"),
                    reason="sell",
                ),
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert [intent.side for intent in intents][:2] == ["SELL", "BUY"]


def test_mode_gating_observe_and_reduce() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True)
    plan = _plan(
        [
            RebalanceAction(
                symbol="BTCTRY",
                side="BUY",
                target_notional_try=Decimal("500"),
                est_qty=Decimal("1"),
                reason="buy",
            ),
            RebalanceAction(
                symbol="BTCTRY",
                side="SELL",
                target_notional_try=Decimal("500"),
                est_qty=Decimal("1"),
                reason="sell",
            ),
        ]
    )
    observe = builder.build_intents(
        cycle_id="c1",
        plan=plan,
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.OBSERVE_ONLY,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    reduce_only = builder.build_intents(
        cycle_id="c1",
        plan=plan,
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.REDUCE_RISK_ONLY,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert observe == []
    assert all(
        intent.side == "SELL" or intent.skip_reason == "mode_reduce_risk_only"
        for intent in reduce_only
    )


def test_spot_sell_requires_inventory_skips_zero_est_qty() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, SPOT_SELL_REQUIRES_INVENTORY=True)
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("100"),
                    est_qty=Decimal("0"),
                    reason="sell",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "spot_sell_requires_inventory"


def test_fee_buffer_reduces_buy_qty_deterministically() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    base_kwargs = dict(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="BUY",
                    target_notional_try=Decimal("1000"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    no_buffer = builder.build_intents(
        settings=Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"), ALLOCATION_FEE_BUFFER_BPS=Decimal("0"), ROUNDING_BUFFER_TRY=Decimal("0")),
        **base_kwargs,
    )[0]
    buffered = builder.build_intents(
        settings=Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"), FEE_BUFFER_RATIO=Decimal("0.01"), ROUNDING_BUFFER_TRY=Decimal("0")),
        **base_kwargs,
    )[0]

    expected_price = rules.quantize_price("BTCTRY", Decimal("100"))
    expected_no_buffer_qty = rules.quantize_qty(
        "BTCTRY", (Decimal("1000") - Decimal("1")) / expected_price
    )
    expected_buffered_qty = rules.quantize_qty(
        "BTCTRY", ((Decimal("1000") * (Decimal("1") - Decimal("0.01"))) - Decimal("1")) / expected_price
    )
    assert no_buffer.qty == expected_no_buffer_qty
    assert buffered.qty == expected_buffered_qty
    assert buffered.notional_try == expected_buffered_qty * expected_price


def test_notional_below_min_total_after_quantize_skip() -> None:
    class CoarsePair(_Pair):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.tickSize = Decimal("1")
            self.stepSize = Decimal("1")
            self.minQuantity = Decimal("1")
            self.minTotalAmount = Decimal("100")

    class CoarseExchange:
        def get_exchange_info(self):
            return [CoarsePair("BTC_TRY")]

    builder = OrderBuilderService()
    rules = ExchangeRulesService(CoarseExchange())
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_ORDER_OFFSET_BPS=Decimal("0"),
        ALLOCATION_FEE_BUFFER_BPS=Decimal("0"),
        ROUNDING_BUFFER_TRY=Decimal("0.00000001"),
    )
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="BUY",
                    target_notional_try=Decimal("101"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("33")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "notional_below_min_total_after_quantize"


def test_buy_happy_path_respects_min_constraints() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_ORDER_OFFSET_BPS=Decimal("0"),
        ALLOCATION_FEE_BUFFER_BPS=Decimal("0"),
        ROUNDING_BUFFER_TRY=Decimal("0.00000001"),
    )
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="BUY",
                    target_notional_try=Decimal("500"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is False
    assert intents[0].qty >= Decimal("0.01")
    assert intents[0].notional_try >= Decimal("100")


def test_qty_below_min_qty_after_quantize_skip() -> None:
    class MinQtyPair(_Pair):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.tickSize = Decimal("1")
            self.stepSize = Decimal("1")
            self.minQuantity = Decimal("2")
            self.minTotalAmount = Decimal("100")

    class MinQtyExchange:
        def get_exchange_info(self):
            return [MinQtyPair("BTC_TRY")]

    builder = OrderBuilderService()
    rules = ExchangeRulesService(MinQtyExchange())
    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STAGE7_ORDER_OFFSET_BPS=Decimal("0"),
        ALLOCATION_FEE_BUFFER_BPS=Decimal("0"),
        ROUNDING_BUFFER_TRY=Decimal("0.00000001"),
    )
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="BUY",
                    target_notional_try=Decimal("150"),
                    est_qty=Decimal("1"),
                    reason="rebalance",
                )
            ]
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "qty_below_min_qty_after_quantize"


def test_sell_missing_balance_skips_with_inventory_reason() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"))
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("500"),
                    est_qty=Decimal("1"),
                    reason="trim",
                )
            ],
            balances={"TRY": {"free": "1000", "locked": "0"}},
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "insufficient_inventory_free_qty"


def test_sell_zero_free_balance_skips_with_inventory_reason() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"))
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("500"),
                    est_qty=Decimal("1"),
                    reason="trim",
                )
            ],
            balances={"BTC": {"free": "0", "locked": "1"}},
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "insufficient_inventory_free_qty"


def test_sell_qty_capped_by_available_inventory_and_quantized() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"))
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("1000"),
                    est_qty=Decimal("5"),
                    reason="trim",
                )
            ],
            balances={"BTC": {"free": "0.239", "locked": "1"}},
        ),
        mark_prices_try={"BTCTRY": Decimal("500")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    expected_price = rules.quantize_price("BTCTRY", Decimal("500"))
    expected_qty = rules.quantize_qty("BTCTRY", Decimal("0.239"))
    assert intents[0].skipped is False
    assert intents[0].price_try == expected_price
    assert intents[0].qty == expected_qty
    assert intents[0].qty <= Decimal("0.239")


def test_sell_qty_below_min_qty_after_quantize_skip() -> None:
    class MinQtySellPair(_Pair):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.minQuantity = Decimal("0.02")
            self.minTotalAmount = Decimal("1")

    class MinQtySellExchange:
        def get_exchange_info(self):
            return [MinQtySellPair("BTC_TRY")]

    builder = OrderBuilderService()
    rules = ExchangeRulesService(MinQtySellExchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"))
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("100"),
                    est_qty=Decimal("0.019"),
                    reason="trim",
                )
            ],
            balances={"BTC": {"free": "0.019", "locked": "0"}},
        ),
        mark_prices_try={"BTCTRY": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "qty_below_min_qty_after_quantize"


def test_sell_notional_below_min_total_after_quantize_skip() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"))
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("50"),
                    est_qty=Decimal("0.5"),
                    reason="trim",
                )
            ],
            balances={"BTC": {"free": "0.5", "locked": "0"}},
        ),
        mark_prices_try={"BTCTRY": Decimal("150")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "notional_below_min_total_after_quantize"


def test_sell_invalid_symbol_skips_for_inventory_resolution() -> None:
    class WeirdQuotePair(_Pair):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.name = "BTCXYZ"
            self.nameNormalized = "BTCXYZ"

    class WeirdQuoteExchange:
        def get_exchange_info(self):
            return [WeirdQuotePair("BTC_XYZ")]

    builder = OrderBuilderService()
    rules = ExchangeRulesService(WeirdQuoteExchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"))
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTC_XYZ",
                    side="SELL",
                    target_notional_try=Decimal("100"),
                    est_qty=Decimal("1"),
                    reason="trim",
                )
            ],
            balances={"BTC": {"free": "2", "locked": "0"}},
        ),
        mark_prices_try={"BTCXYZ": Decimal("100")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    assert intents[0].skipped is True
    assert intents[0].skip_reason == "invalid_symbol_for_inventory_resolution"


def test_sell_quantization_uses_rules_quantizer_authority() -> None:
    builder = OrderBuilderService()
    rules = ExchangeRulesService(_Exchange())
    settings = Settings(DRY_RUN=True, STAGE7_ENABLED=True, STAGE7_ORDER_OFFSET_BPS=Decimal("0"))
    est_qty = Decimal("0.239999")
    intents = builder.build_intents(
        cycle_id="c1",
        plan=_plan(
            [
                RebalanceAction(
                    symbol="BTCTRY",
                    side="SELL",
                    target_notional_try=Decimal("1000"),
                    est_qty=est_qty,
                    reason="trim",
                )
            ],
            balances={"BTC": {"free": "0.239999", "locked": "0"}},
        ),
        mark_prices_try={"BTCTRY": Decimal("500")},
        rules=rules,
        settings=settings,
        final_mode=Mode.NORMAL,
        now_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )

    expected_qty = rules.quantize_qty("BTCTRY", est_qty)
    assert intents[0].skipped is False
    assert intents[0].qty == expected_qty

