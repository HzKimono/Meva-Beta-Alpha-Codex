from __future__ import annotations

from decimal import Decimal

from btcbot.domain.strategy_core import OrderBookSummary
from btcbot.domain.universe_models import SymbolInfo, UniverseKnobs
from btcbot.services.universe_service import select_universe


def test_allow_list_only() -> None:
    symbols = [
        SymbolInfo(symbol="btc_try", base="BTC", quote="TRY"),
        SymbolInfo(symbol="eth_try", base="ETH", quote="TRY"),
    ]

    result = select_universe(
        symbols=symbols,
        orderbooks=None,
        knobs=UniverseKnobs(allow_symbols=("btc_try",)),
    )

    assert result == ["BTCTRY"]


def test_deny_list_removes_symbol() -> None:
    symbols = [
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY"),
        SymbolInfo(symbol="ETH_TRY", base="ETH", quote="TRY"),
    ]

    result = select_universe(
        symbols=symbols,
        orderbooks=None,
        knobs=UniverseKnobs(deny_symbols=("ethtry",)),
    )

    assert result == ["BTCTRY"]


def test_quote_try_filter() -> None:
    symbols = [
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY"),
        SymbolInfo(symbol="BTC_USDT", base="BTC", quote="USDT"),
    ]

    result = select_universe(symbols=symbols, orderbooks=None, knobs=UniverseKnobs())

    assert result == ["BTCTRY"]


def test_active_filter() -> None:
    symbols = [
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY", active=False),
        SymbolInfo(symbol="ETH_TRY", base="ETH", quote="TRY", active=True),
    ]

    result = select_universe(symbols=symbols, orderbooks=None, knobs=UniverseKnobs())

    assert result == ["ETHTRY"]


def test_exchange_min_total_filter_rejects_high_min_total() -> None:
    symbols = [
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY", min_notional_try=Decimal("150")),
        SymbolInfo(symbol="ETH_TRY", base="ETH", quote="TRY", min_notional_try=Decimal("100")),
    ]

    result = select_universe(
        symbols=symbols,
        orderbooks=None,
        knobs=UniverseKnobs(max_exchange_min_total_try=Decimal("120")),
    )

    assert result == ["ETHTRY"]


def test_low_price_does_not_get_rejected_by_min_notional_knob() -> None:
    symbols = [
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY", min_notional_try=Decimal("100")),
    ]
    orderbooks = {
        "BTC_TRY": OrderBookSummary(best_bid=Decimal("0.09"), best_ask=Decimal("0.1")),
    }

    result = select_universe(
        symbols=symbols,
        orderbooks=orderbooks,
        knobs=UniverseKnobs(
            min_notional_try=Decimal("100000"),
            max_exchange_min_total_try=Decimal("120"),
            max_spread_bps=Decimal("2000"),
        ),
    )

    assert result == ["BTCTRY"]


def test_spread_filter_rejects_above_threshold() -> None:
    symbols = [SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY")]
    orderbooks = {"BTCTRY": OrderBookSummary(best_bid=Decimal("90"), best_ask=Decimal("110"))}

    result = select_universe(
        symbols=symbols,
        orderbooks=orderbooks,
        knobs=UniverseKnobs(max_spread_bps=Decimal("500")),
    )

    assert result == []


def test_spread_filter_accepts_within_threshold() -> None:
    symbols = [SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY")]
    orderbooks = {"BTCTRY": OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("101"))}

    result = select_universe(symbols=symbols, orderbooks=orderbooks, knobs=UniverseKnobs())

    assert result == ["BTCTRY"]


def test_ranking_spread_then_volume_then_symbol() -> None:
    symbols = [
        SymbolInfo(symbol="ETH_TRY", base="ETH", quote="TRY", volume_try=Decimal("2000")),
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY", volume_try=Decimal("1000")),
        SymbolInfo(symbol="ADA_TRY", base="ADA", quote="TRY", volume_try=Decimal("2000")),
    ]
    orderbooks = {
        "ETHTRY": OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("100")),
        "BTCTRY": OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("100")),
        "ADATRY": OrderBookSummary(best_bid=Decimal("99.5"), best_ask=Decimal("100")),
    }

    result = select_universe(symbols=symbols, orderbooks=orderbooks, knobs=UniverseKnobs())

    assert result == ["ADATRY", "ETHTRY", "BTCTRY"]


def test_deterministic_with_order_variations() -> None:
    symbols_a = [
        SymbolInfo(symbol="ETH_TRY", base="ETH", quote="TRY"),
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY"),
    ]
    symbols_b = [
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY"),
        SymbolInfo(symbol="ETH_TRY", base="ETH", quote="TRY"),
    ]
    orderbooks_a = {
        "ETHTRY": OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("100")),
        "BTCTRY": OrderBookSummary(best_bid=Decimal("99.5"), best_ask=Decimal("100")),
    }
    orderbooks_b = {
        "BTCTRY": OrderBookSummary(best_bid=Decimal("99.5"), best_ask=Decimal("100")),
        "ETHTRY": OrderBookSummary(best_bid=Decimal("99"), best_ask=Decimal("100")),
    }

    first = select_universe(symbols=symbols_a, orderbooks=orderbooks_a, knobs=UniverseKnobs())
    second = select_universe(symbols=symbols_b, orderbooks=orderbooks_b, knobs=UniverseKnobs())

    assert first == second
    assert first == ["BTCTRY", "ETHTRY"]


def test_universe_excludes_symbols_with_active_cooldown() -> None:
    symbols = [
        SymbolInfo(symbol="BTC_TRY", base="BTC", quote="TRY"),
        SymbolInfo(symbol="ETH_TRY", base="ETH", quote="TRY"),
    ]

    result = select_universe(
        symbols=symbols,
        orderbooks=None,
        knobs=UniverseKnobs(),
        active_cooldowns={"BTCTRY": {"cooldown_until_ts": 9999999999}},
    )

    assert result == ["ETHTRY"]
