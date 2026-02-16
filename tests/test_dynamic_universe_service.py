from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.services.decision_pipeline_service import DecisionPipelineService
from btcbot.services.dynamic_universe_service import DynamicUniverseService
from btcbot.services.state_store import StateStore


class _MockClient:
    def __init__(
        self, pair_symbols: list[str], books: dict[str, tuple[str, str, str, str]]
    ) -> None:
        self._pair_symbols = pair_symbols
        self._books = books

    def get_exchange_info(self) -> list[PairInfo]:
        return [
            PairInfo(
                pairSymbol=symbol,
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                status="TRADING",
            )
            for symbol in self._pair_symbols
        ]

    def _get(self, path: str, params: dict[str, object]) -> dict[str, object]:
        assert path == "/api/v2/orderbook"
        symbol = str(params["pairSymbol"])
        bid_price, bid_qty, ask_price, ask_qty = self._books[symbol]
        return {
            "data": {
                "bids": [[bid_price, bid_qty]],
                "asks": [[ask_price, ask_qty]],
            }
        }


class _MockExchange:
    def __init__(self, client: _MockClient) -> None:
        self.client = client


def test_dynamic_universe_picks_top5_momentum_with_filters(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "state.db"
    store = StateStore(db_path=str(db_path))
    service = DynamicUniverseService()

    symbols = ["AAAATRY", "BBBBTRY", "CCCCTRY", "DDDDTRY", "EEEETRY", "FFFFTRY", "USDTTRY"]
    books = {
        "AAAATRY": ("120", "500", "121", "500"),
        "BBBBTRY": ("110", "500", "111", "500"),
        "CCCCTRY": ("108", "500", "109", "500"),
        "DDDDTRY": ("105", "500", "106", "500"),
        "EEEETRY": ("102", "500", "103", "500"),
        "FFFFTRY": ("95", "500", "96", "500"),
        "USDTTRY": ("40", "1000", "41", "1000"),
    }
    exchange = _MockExchange(_MockClient(symbols, books))

    lookback_bucket = now - timedelta(hours=24)
    for symbol, price in {
        "AAAATRY": "100",
        "BBBBTRY": "100",
        "CCCCTRY": "100",
        "DDDDTRY": "100",
        "EEEETRY": "100",
        "FFFFTRY": "100",
        "USDTTRY": "100",
    }.items():
        store.upsert_universe_price_snapshot(
            pair_symbol=symbol,
            ts_bucket=lookback_bucket,
            mid_price=Decimal(price),
        )

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        SYMBOLS="[]",
        UNIVERSE_TOP_N=5,
        UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
        UNIVERSE_MIN_DEPTH_TRY=Decimal("10000"),
        UNIVERSE_EXCLUDE_STABLES=True,
        UNIVERSE_EXCLUDE_SYMBOLS='["USDTTRY"]',
    )
    result = service.select(
        exchange=exchange,
        state_store=store,
        settings=settings,
        now_utc=now,
        cycle_id="cycle-1",
    )

    assert result.selected_symbols == (
        "AAAATRY",
        "BBBBTRY",
        "CCCCTRY",
        "DDDDTRY",
        "EEEETRY",
    )
    assert result.ineligible_counts["excluded_symbol"] == 1


def test_dynamic_universe_marks_insufficient_history(tmp_path) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    exchange = _MockExchange(
        _MockClient(
            ["AAAATRY", "BBBBTRY"],
            {
                "AAAATRY": ("100", "800", "101", "800"),
                "BBBBTRY": ("100", "800", "101", "800"),
            },
        )
    )
    store.upsert_universe_price_snapshot(
        pair_symbol="AAAATRY",
        ts_bucket=now - timedelta(hours=24),
        mid_price=Decimal("90"),
    )

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        SYMBOLS="[]",
        UNIVERSE_SPREAD_MAX_BPS=Decimal("200"),
    )
    result = DynamicUniverseService().select(
        exchange=exchange,
        state_store=store,
        settings=settings,
        now_utc=now,
        cycle_id="cycle-2",
    )

    assert "AAAATRY" in result.selected_symbols
    assert result.ineligible_counts["insufficient_history"] == 1


def test_aggressive_allocation_respects_cash_target_invariant() -> None:
    service = DecisionPipelineService(
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            TRY_CASH_TARGET=Decimal("300"),
            FEE_BUFFER_RATIO=Decimal("0.002"),
        ),
        now_provider=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    pair_info = [
        PairInfo(pairSymbol=f"S{i}TRY", numeratorScale=6, denominatorScale=2, minTotalAmount=10)
        for i in range(1, 6)
    ]
    scores = {f"S{i}TRY": Decimal(str(i)) for i in range(1, 6)}
    mark_prices = {f"S{i}TRY": Decimal("100") for i in range(1, 6)}

    report = service.run_cycle(
        cycle_id="cycle-3",
        balances={"TRY": Decimal("1300")},
        positions={},
        mark_prices=mark_prices,
        open_orders=[],
        pair_info=pair_info,
        bootstrap_enabled=True,
        live_mode=False,
        preferred_symbols=sorted(mark_prices.keys()),
        aggressive_scores=scores,
    )

    planned_plus_fees = report.planned_total_try * (Decimal("1") + Decimal("0.002"))
    remaining_cash = report.cash_try - planned_plus_fees
    assert report.planned_total_try > Decimal("0")
    assert remaining_cash >= Decimal("300")


def test_aggressive_allocation_drops_min_notional_and_renormalizes() -> None:
    service = DecisionPipelineService(
        settings=Settings(
            DRY_RUN=True,
            KILL_SWITCH=False,
            SYMBOLS="[]",
            TRY_CASH_TARGET=Decimal("0"),
        ),
        now_provider=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    pair_info = [
        PairInfo(pairSymbol="BIGTRY", numeratorScale=6, denominatorScale=2, minTotalAmount=10),
        PairInfo(pairSymbol="SMALLTRY", numeratorScale=6, denominatorScale=2, minTotalAmount=80),
    ]

    report = service.run_cycle(
        cycle_id="cycle-4",
        balances={"TRY": Decimal("100")},
        positions={},
        mark_prices={"BIGTRY": Decimal("10"), "SMALLTRY": Decimal("10")},
        open_orders=[],
        pair_info=pair_info,
        bootstrap_enabled=True,
        live_mode=False,
        preferred_symbols=["BIGTRY", "SMALLTRY"],
        aggressive_scores={"BIGTRY": Decimal("0.9"), "SMALLTRY": Decimal("0.1")},
    )

    symbols = {order.symbol for order in report.order_requests}
    assert symbols == {"BIGTRY"}
    assert report.dropped_reasons["dropped_min_notional"] == 1
