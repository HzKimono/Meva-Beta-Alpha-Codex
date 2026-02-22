from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.domain.decision_codes import ReasonCode
from btcbot.domain.intent import Intent
from btcbot.domain.models import OrderSide, PairInfo, SymbolRules
from btcbot.logging_utils import JsonFormatter
from btcbot.risk.exchange_rules import (
    ExchangeRules,
    ExchangeRulesUnavailableError,
    MarketDataExchangeRulesProvider,
)
from btcbot.risk.policy import RiskPolicy, RiskPolicyContext
from btcbot.services.market_data_service import MarketDataService


class StaticRules:
    def get_rules(self, symbol: str) -> ExchangeRules:
        del symbol
        return ExchangeRules(
            min_notional=Decimal("10"),
            price_tick=Decimal("0.1"),
            qty_step=Decimal("0.01"),
        )


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class CountingMarketDataService:
    def __init__(self) -> None:
        self.calls = 0

    def get_symbol_rules(self, symbol: str) -> SymbolRules:
        self.calls += 1
        return SymbolRules(
            pair_symbol=symbol,
            price_scale=2,
            quantity_scale=8,
            min_total=Decimal("10"),
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00000001"),
        )


class FailingMarketDataService:
    def __init__(self) -> None:
        self.calls = 0

    def get_symbol_rules(self, symbol: str):
        del symbol
        self.calls += 1
        raise RuntimeError("rules unavailable")


class FakeExchangeWithRules:
    def get_exchange_info(self) -> list[PairInfo]:
        return [
            PairInfo(
                pairSymbol="BTC_TRY",
                numeratorScale=8,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                tickSize=Decimal("0.1"),
                stepSize=Decimal("0.0001"),
            ),
            PairInfo(
                pairSymbol="ETH_TRY",
                numeratorScale=8,
                denominatorScale=2,
                minTotalAmount=Decimal("20"),
                tickSize=Decimal("0.01"),
                stepSize=Decimal("0.001"),
            ),
        ]


def _intent(reason: str = "test") -> Intent:
    return Intent.create(
        cycle_id="c1",
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        qty=Decimal("0.2"),
        limit_price=Decimal("100"),
        reason=reason,
    )


def test_policy_quantizes_and_filters() -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    policy = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=2,
        max_open_orders_per_symbol=1,
        cooldown_seconds=60,
        notional_cap_try_per_cycle=Decimal("50"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=clock.now,
    )
    intents = [
        Intent.create(
            cycle_id="c1",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            qty=Decimal("0.123"),
            limit_price=Decimal("100.17"),
            reason="test",
        ),
        Intent.create(
            cycle_id="c1",
            symbol="ETH_TRY",
            side=OrderSide.BUY,
            qty=Decimal("0.05"),
            limit_price=Decimal("100"),
            reason="too_small",
        ),
    ]
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={"ETHTRY": 1},
        last_intent_ts_by_symbol_side={
            ("BTCTRY", "buy"): clock.now() - timedelta(seconds=120),
        },
        mark_prices={},
    )

    approved = policy.evaluate(context, intents)
    assert len(approved) == 1
    assert approved[0].limit_price == Decimal("100.1")
    assert approved[0].qty == Decimal("0.12")


def test_policy_cooldown_allows_when_zero() -> None:
    clock = FixedClock(datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
    policy = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=1,
        max_open_orders_per_symbol=1,
        cooldown_seconds=0,
        notional_cap_try_per_cycle=Decimal("100"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=clock.now,
    )
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={("BTCTRY", "buy"): clock.now() - timedelta(seconds=1)},
        mark_prices={},
    )

    approved = policy.evaluate(context, [_intent("cooldown_zero")])
    assert len(approved) == 1


def test_policy_blocks_when_within_positive_cooldown_window() -> None:
    clock = FixedClock(datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
    policy = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=1,
        max_open_orders_per_symbol=1,
        cooldown_seconds=60,
        notional_cap_try_per_cycle=Decimal("100"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=clock.now,
    )
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={("BTCTRY", "buy"): clock.now() - timedelta(seconds=10)},
        mark_prices={},
    )

    assert policy.evaluate(context, [_intent("cooldown_block")]) == []


def test_policy_cooldown_is_deterministic_with_injected_clock() -> None:
    frozen_now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    policy_a = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=1,
        max_open_orders_per_symbol=1,
        cooldown_seconds=60,
        notional_cap_try_per_cycle=Decimal("100"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=lambda: frozen_now,
    )
    policy_b = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=1,
        max_open_orders_per_symbol=1,
        cooldown_seconds=60,
        notional_cap_try_per_cycle=Decimal("100"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=lambda: frozen_now,
    )
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={("BTCTRY", "buy"): frozen_now - timedelta(seconds=10)},
        mark_prices={},
    )

    assert policy_a.evaluate(context, [_intent("deterministic")]) == []
    assert policy_b.evaluate(context, [_intent("deterministic")]) == []


def test_market_data_rules_provider_uses_ttl_cache() -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    service = CountingMarketDataService()
    provider = MarketDataExchangeRulesProvider(
        service,
        cache_ttl_seconds=600,
        now_provider=clock.now,
    )

    first = provider.get_rules("BTC_TRY")
    second = provider.get_rules("BTCTRY")

    assert first == second
    assert service.calls == 1


def test_market_data_rules_provider_returns_defaults_on_error() -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    service = FailingMarketDataService()
    provider = MarketDataExchangeRulesProvider(
        service,
        cache_ttl_seconds=600,
        now_provider=clock.now,
    )

    rules = provider.get_rules("BTC_TRY")

    assert rules.min_notional == Decimal("10")
    assert rules.price_tick == Decimal("0.01")
    assert rules.qty_step == Decimal("0.00000001")
    assert service.calls == 1


def test_policy_allows_evaluation_when_rules_unavailable() -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    policy = RiskPolicy(
        rules_provider=MarketDataExchangeRulesProvider(
            FailingMarketDataService(),
            now_provider=clock.now,
        ),
        max_orders_per_cycle=1,
        max_open_orders_per_symbol=1,
        cooldown_seconds=60,
        notional_cap_try_per_cycle=Decimal("100"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=clock.now,
    )
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={},
        mark_prices={},
    )

    approved = policy.evaluate(context, [_intent("fallback_rules")])

    assert len(approved) == 1


def test_market_data_rules_provider_fail_closed_when_defaults_disabled(caplog) -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    provider = MarketDataExchangeRulesProvider(
        FailingMarketDataService(),
        now_provider=clock.now,
        allow_default_fallback=False,
    )

    caplog.set_level(logging.ERROR, logger="btcbot.risk.exchange_rules")
    try:
        provider.get_rules("BTC_TRY")
        raise AssertionError("expected fail-closed rules exception")
    except ExchangeRulesUnavailableError:
        pass

    assert any(
        record.getMessage() == "exchange_rules_missing_fail_closed" for record in caplog.records
    )


def test_policy_rejects_intents_when_exchange_rules_unavailable_fail_closed(
    caplog, monkeypatch
) -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    policy = RiskPolicy(
        rules_provider=MarketDataExchangeRulesProvider(
            FailingMarketDataService(),
            now_provider=clock.now,
            allow_default_fallback=False,
        ),
        max_orders_per_cycle=1,
        max_open_orders_per_symbol=1,
        cooldown_seconds=60,
        notional_cap_try_per_cycle=Decimal("100"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=clock.now,
    )
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={},
        mark_prices={},
    )

    captured: list[dict[str, object]] = []

    def _capture_decision(logger, payload):
        del logger
        captured.append(payload)

    monkeypatch.setattr("btcbot.risk.policy.emit_decision", _capture_decision)
    caplog.set_level(logging.WARNING, logger="btcbot.risk.policy")
    approved = policy.evaluate(context, [_intent("rules_missing")])

    assert approved == []
    assert any(
        str(event.get("reason_code")) == "exchange_rules_unavailable_blocked"
        for event in policy.last_blocked_events
    )
    assert captured
    assert captured[-1]["decision_layer"] == "risk_policy"
    assert captured[-1]["reason_code"] == "exchange_rules_unavailable_blocked"
    assert captured[-1]["action"] == "BLOCK"
    assert any(
        record.getMessage() == "exchange_rules_unavailable_blocked" for record in caplog.records
    )


def test_market_data_rules_provider_logs_traceback_on_fallback(caplog) -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    provider = MarketDataExchangeRulesProvider(FailingMarketDataService(), now_provider=clock.now)

    caplog.set_level(logging.WARNING, logger="btcbot.risk.exchange_rules")
    provider.get_rules("BTC_TRY")

    assert caplog.records
    payload = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert payload["message"] == "Exchange rules unavailable; using defaults"
    assert payload["error_type"] == "RuntimeError"
    assert "traceback" in payload
    assert "RuntimeError: rules unavailable" in payload["traceback"]


def test_market_data_rules_provider_returns_non_default_rules_from_exchange_info() -> None:
    market_data_service = MarketDataService(exchange=FakeExchangeWithRules())
    provider = MarketDataExchangeRulesProvider(market_data_service)

    btc_rules = provider.get_rules("BTCTRY")
    eth_rules = provider.get_rules("ETHTRY")

    assert btc_rules.min_notional == Decimal("10")
    assert btc_rules.price_tick == Decimal("0.1")
    assert btc_rules.qty_step == Decimal("0.0001")
    assert eth_rules.min_notional == Decimal("20")
    assert eth_rules.price_tick == Decimal("0.01")
    assert eth_rules.qty_step == Decimal("0.001")


def test_market_data_rules_provider_uses_market_rules_without_fallback_warning(caplog) -> None:
    market_data_service = MarketDataService(exchange=FakeExchangeWithRules())
    provider = MarketDataExchangeRulesProvider(market_data_service)

    caplog.set_level(logging.WARNING, logger="btcbot.risk.exchange_rules")
    rules = provider.get_rules("BTCTRY")

    assert rules.min_notional == Decimal("10")
    assert not any(
        record.getMessage() == "Exchange rules unavailable; using defaults"
        for record in caplog.records
    )


def test_exchange_rules_provider_resolves_underscore_and_canonical_same() -> None:
    market_data_service = MarketDataService(exchange=FakeExchangeWithRules())
    provider = MarketDataExchangeRulesProvider(market_data_service)

    canonical = provider.get_rules("BTCTRY")
    underscore = provider.get_rules("BTC_TRY")

    assert canonical == underscore


def test_policy_notional_cap_logs_block_math(caplog) -> None:
    clock = FixedClock(datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
    policy = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=2,
        max_open_orders_per_symbol=2,
        cooldown_seconds=0,
        notional_cap_try_per_cycle=Decimal("15"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=clock.now,
    )
    caplog.set_level(logging.INFO, logger="btcbot.risk.policy")
    intents = [
        Intent.create(
            cycle_id="c1",
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            qty=Decimal("0.1"),
            limit_price=Decimal("100"),
            reason="first",
        ),
        Intent.create(
            cycle_id="c1",
            symbol="ETH_TRY",
            side=OrderSide.BUY,
            qty=Decimal("0.1"),
            limit_price=Decimal("100"),
            reason="second",
        ),
    ]
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={},
        mark_prices={},
        cash_try_free=Decimal("320"),
        try_cash_target=Decimal("300"),
        investable_try=Decimal("20"),
    )

    approved = policy.evaluate(context, intents)

    assert len(approved) == 1
    blocked = [r for r in caplog.records if r.getMessage() == "Intent blocked by risk policy"]
    assert blocked
    payload = json.loads(JsonFormatter().format(blocked[-1]))
    assert payload["reason"] == str(ReasonCode.RISK_BLOCK_NOTIONAL_CAP)
    assert payload["reason_code"] == str(ReasonCode.RISK_BLOCK_NOTIONAL_CAP)
    assert payload["cap_try_per_cycle"] == "15"
    assert payload["intent_notional_try"] == "10.0"
    assert payload["used_notional_try"] == "10.0"
    assert payload["cash_try_free"] == "320"
    assert payload["try_cash_target"] == "300"
    assert payload["investable_try"] == "20"
    assert payload["rule"] == str(ReasonCode.RISK_BLOCK_NOTIONAL_CAP)
    assert payload["planned_spend_try"] == "20.0"

    decision_events = [r for r in caplog.records if r.getMessage() == "decision_event"]
    assert decision_events
    decision_payload = json.loads(JsonFormatter().format(decision_events[-1]))
    assert decision_payload["reason_code"] in {
        str(ReasonCode.RISK_BLOCK_NOTIONAL_CAP),
        str(ReasonCode.RISK_BLOCK_CASH_RESERVE_TARGET),
    }
    assert getattr(decision_events[-1], "extra", {}).get("cycle_id") == "c1"


def test_cash_reserve_target_allows_intent_when_free_cash_above_target() -> None:
    policy = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=2,
        max_open_orders_per_symbol=2,
        cooldown_seconds=0,
        notional_cap_try_per_cycle=Decimal("10000"),
        max_notional_per_order_try=Decimal("0"),
    )
    context = RiskPolicyContext(
        cycle_id="c-free-cash",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={},
        mark_prices={},
        cash_try_free=Decimal("1306"),
        try_cash_target=Decimal("300"),
        investable_try=Decimal("1006"),
    )
    intent = Intent.create(
        cycle_id="c-free-cash",
        symbol="BTCTRY",
        side=OrderSide.BUY,
        qty=Decimal("1"),
        limit_price=Decimal("100"),
        reason="cash reserve should not block",
    )

    approved = policy.evaluate(context, [intent])

    assert len(approved) == 1


def test_cash_reserve_target_blocks_buy_but_not_sell() -> None:
    policy = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=3,
        max_open_orders_per_symbol=2,
        cooldown_seconds=0,
        notional_cap_try_per_cycle=Decimal("10000"),
        max_notional_per_order_try=Decimal("0"),
    )
    context = RiskPolicyContext(
        cycle_id="c-cash-target",
        open_orders_by_symbol={},
        last_intent_ts_by_symbol_side={},
        mark_prices={},
        cash_try_free=Decimal("300"),
        try_cash_target=Decimal("500"),
        investable_try=Decimal("50"),
    )
    buy_intent = Intent.create(
        cycle_id="c-cash-target",
        symbol="BTCTRY",
        side=OrderSide.BUY,
        qty=Decimal("1"),
        limit_price=Decimal("100"),
        reason="buy should be blocked by cash reserve target",
    )
    sell_intent = Intent.create(
        cycle_id="c-cash-target",
        symbol="BTCTRY",
        side=OrderSide.SELL,
        qty=Decimal("1"),
        limit_price=Decimal("100"),
        reason="sell should pass because it increases free cash",
    )

    approved = policy.evaluate(context, [buy_intent, sell_intent])

    assert [intent.side for intent in approved] == [OrderSide.SELL]


def test_policy_block_log_includes_symbol_side_and_open_order_identifiers(caplog) -> None:
    clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
    policy = RiskPolicy(
        rules_provider=StaticRules(),
        max_orders_per_cycle=1,
        max_open_orders_per_symbol=1,
        cooldown_seconds=0,
        notional_cap_try_per_cycle=Decimal("100"),
        max_notional_per_order_try=Decimal("0"),
        now_provider=clock.now,
    )
    context = RiskPolicyContext(
        cycle_id="c1",
        open_orders_by_symbol={"BTCTRY": 1},
        open_order_identifiers_by_symbol={"BTCTRY": ["oid-1", "oid-2"]},
        last_intent_ts_by_symbol_side={},
        mark_prices={},
    )

    caplog.set_level(logging.INFO)
    assert policy.evaluate(context, [_intent("max-open")]) == []

    rec = next(r for r in caplog.records if r.getMessage() == "Intent blocked by risk policy")
    payload = getattr(rec, "extra", {})
    assert payload["symbol"] == "BTCTRY"
    assert payload["side"] == "buy"
    assert payload["open_orders_for_symbol"] == "1"
    assert payload["open_order_identifiers"] == ["oid-1", "oid-2"]
    assert payload["open_orders_count_origin"] == "reconciled"
    assert "client_order_id" in payload
