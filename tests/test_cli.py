from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime
from decimal import Decimal

from btcbot import cli
from btcbot.adapters.action_to_order import build_exchange_rules
from btcbot.adapters.btcturk_http import (
    ConfigurationError,
    DryRunExchangeClient,
    DryRunExchangeClientStage4,
)
from btcbot.config import Settings
from btcbot.domain.accounting import TradeFill
from btcbot.domain.intent import Intent
from btcbot.domain.models import Balance, OrderSide, PairInfo
from btcbot.domain.stage4 import Quantizer
from btcbot.logging_utils import JsonFormatter
from btcbot.risk.exchange_rules import ExchangeRules
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner, Stage4InvariantError


class _Freshness:
    def __init__(
        self,
        *,
        is_stale: bool = False,
        observed_age_ms: int = 0,
        max_age_ms: int = 15_000,
        source_mode: str = "rest",
        connected: bool = True,
        missing_symbols: list[str] | None = None,
    ) -> None:
        self.is_stale = is_stale
        self.observed_age_ms = observed_age_ms
        self.max_age_ms = max_age_ms
        self.source_mode = source_mode
        self.connected = connected
        self.missing_symbols = list(missing_symbols or [])


class HealthyClient:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    def close(self) -> None:
        return None

    def health_check(self) -> bool:
        return True


class UnhealthyClient(HealthyClient):
    def health_check(self) -> bool:
        return False


class UnreachableClient(HealthyClient):
    def health_check(self) -> bool:
        raise RuntimeError("network unreachable")


def test_health_returns_zero_on_success(monkeypatch) -> None:
    monkeypatch.setattr(cli, "BtcturkHttpClient", HealthyClient)
    settings = Settings()

    assert cli.run_health(settings) == 0


def test_health_returns_nonzero_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(cli, "BtcturkHttpClient", UnhealthyClient)
    settings = Settings()

    assert cli.run_health(settings) == 1


def test_health_prints_effective_risk_config(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "BtcturkHttpClient", HealthyClient)
    settings = Settings()

    assert cli.run_health(settings) == 0
    out = capsys.readouterr().out
    assert "Effective risk config:" in out
    assert "TRY_CASH_TARGET=300" in out
    assert "NOTIONAL_CAP_TRY_PER_CYCLE=1000" in out


def test_health_prints_effective_risk_config_on_unreachable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "BtcturkHttpClient", UnreachableClient)
    settings = Settings(DRY_RUN=True)

    assert cli.run_health(settings) == 0
    out = capsys.readouterr().out
    assert "SKIP (unreachable in current environment)" in out
    assert "Effective risk config:" in out


def test_run_dry_run_does_not_crash_with_missing_market_data(monkeypatch) -> None:
    class FailingPublicClient(HealthyClient):
        def get_orderbook(self, symbol: str, limit: int | None = None):
            del symbol, limit
            raise RuntimeError("temporary failure")

    monkeypatch.setattr(cli, "BtcturkHttpClient", FailingPublicClient)

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=True,
        SYMBOLS="BTC_TRY,ETH_TRY",
        TARGET_TRY=300,
        DRY_RUN_TRY_BALANCE=500,
    )

    assert cli.run_cycle(settings, force_dry_run=True) == 0


def test_run_cycle_fails_when_dry_run_disabled(capsys) -> None:
    settings = Settings(DRY_RUN=False, KILL_SWITCH=True, SAFE_MODE=False)

    code = cli.run_cycle(settings, force_dry_run=False)

    out = capsys.readouterr().out
    assert code == 2
    assert out.strip() == "KILL_SWITCH=true blocks side effects"


def test_run_cycle_live_not_armed_message_is_specific(capsys) -> None:
    settings = Settings(DRY_RUN=False, KILL_SWITCH=False, LIVE_TRADING=False, SAFE_MODE=False)

    code = cli.run_cycle(settings, force_dry_run=False)

    out = capsys.readouterr().out
    assert code == 2
    assert out.strip() == cli.LIVE_TRADING_NOT_ARMED_MESSAGE


def test_run_cycle_wires_services_and_persists_last_cycle_id(monkeypatch) -> None:
    events: list[str] = []

    class FakeExchange:
        def close(self) -> None:
            events.append("exchange.close")

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            self.db_path = db_path
            self.last_cycle_id: str | None = None
            events.append(f"state.init:{db_path}")

        def set_last_cycle_id(self, cycle_id: str) -> None:
            self.last_cycle_id = cycle_id
            events.append("state.set_last_cycle_id")

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            events.append("portfolio.get_balances")
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            events.append("market.get_best_bids")
            return {}, _Freshness(max_age_ms=max_age_ms)

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            events.append("sweep.build")
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            events.append("execution.cancel_stale")
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents, cycle_id
            events.append("execution.execute")
            return 0

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: FakeExchange(),
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=True)
    code = cli.run_cycle(settings, force_dry_run=True)

    assert code == 0
    assert "execution.cancel_stale" in events
    assert "sweep.build" in events
    assert "execution.execute" in events
    assert "state.set_last_cycle_id" in events
    assert events[-1] == "exchange.close"


def test_run_cycle_dry_run_emits_decision_event_with_envelope_keys(monkeypatch, caplog) -> None:
    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            del cycle_id

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            return {}, _Freshness(max_age_ms=max_age_ms)

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            del kwargs
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents, cycle_id
            return 0

    class FakeRiskService:
        def __init__(self, risk_policy, state_store) -> None:
            del risk_policy, state_store

        def filter(self, cycle_id: str, intents, **kwargs):
            del cycle_id, kwargs
            return intents

    monkeypatch.setattr(
        cli, "build_exchange_stage3", lambda settings, force_dry_run: FakeExchange()
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)
    monkeypatch.setattr(cli, "RiskService", FakeRiskService)

    caplog.set_level("INFO", logger="btcbot.cli")
    settings = Settings(DRY_RUN=True, KILL_SWITCH=False, SAFE_MODE=False)
    assert cli.run_cycle(settings, force_dry_run=True) == 0

    decision_events = [
        record for record in caplog.records if record.getMessage() == "decision_event"
    ]
    assert decision_events
    payload = json.loads(JsonFormatter().format(decision_events[0]))
    for key in ("cycle_id", "decision_layer", "reason_code", "action"):
        assert key in payload


def test_run_cycle_returns_two_on_configuration_error(monkeypatch) -> None:
    class BrokenExchange:
        def close(self) -> None:
            return None

    class BrokenPortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            raise ConfigurationError("Missing BTCTURK_API_KEY")

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: BrokenExchange(),
    )
    monkeypatch.setattr(cli, "PortfolioService", BrokenPortfolioService)

    settings = Settings(
        DRY_RUN=False,
        KILL_SWITCH=False,
        LIVE_TRADING=True,
        SAFE_MODE=False,
        LIVE_TRADING_ACK="I_UNDERSTAND",
        BTCTURK_API_KEY="key",
        BTCTURK_API_SECRET="secret",
    )

    assert cli.run_cycle(settings, force_dry_run=False) == 2


def test_run_cycle_close_failure_does_not_mask_error(monkeypatch) -> None:
    class FailingExchange:
        def close(self) -> None:
            raise RuntimeError("close failed")

    class BrokenPortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: FailingExchange(),
    )
    monkeypatch.setattr(cli, "PortfolioService", BrokenPortfolioService)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=False)

    assert cli.run_cycle(settings, force_dry_run=True) == 1


def test_run_cycle_uses_unique_cycle_ids(monkeypatch) -> None:
    captured: list[str] = []

    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            captured.append(cycle_id)

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            return {}, _Freshness(max_age_ms=max_age_ms)

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            del kwargs
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents, cycle_id
            return 0

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: FakeExchange(),
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=True)
    assert cli.run_cycle(settings, force_dry_run=True) == 0
    assert cli.run_cycle(settings, force_dry_run=True) == 0

    assert len(captured) == 2
    assert captured[0] != captured[1]
    assert all(len(cycle_id) == 32 for cycle_id in captured)


def test_run_cycle_normalizes_mark_price_keys(monkeypatch) -> None:
    captured_mark_prices: dict[str, object] = {}

    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            del cycle_id

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            return {"btc_try": 100.0}, _Freshness(max_age_ms=max_age_ms)

    class FakeAccountingService:
        def __init__(self, exchange, state_store) -> None:
            del exchange, state_store

        def refresh(self, symbols, mark_prices):
            del symbols
            captured_mark_prices.update(mark_prices)
            return 0

        def get_positions(self):
            return []

    class FakeStrategyService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def generate(self, **kwargs):
            del kwargs
            return []

    class FakeRiskService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter(self, cycle_id, intents, **kwargs):
            del cycle_id, kwargs
            return intents

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            del kwargs
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents, cycle_id
            return 0

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: FakeExchange(),
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "AccountingService", FakeAccountingService)
    monkeypatch.setattr(cli, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(cli, "RiskService", FakeRiskService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=True, SYMBOLS="BTC_TRY")

    assert cli.run_cycle(settings, force_dry_run=True) == 0
    assert "BTCTRY" in captured_mark_prices


def test_run_cycle_passes_cycle_id_to_execution(monkeypatch) -> None:
    captured_cycle_ids: list[str] = []

    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            del cycle_id

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            return {}, _Freshness(max_age_ms=max_age_ms)

    class FakeAccountingService:
        def __init__(self, exchange, state_store) -> None:
            del exchange, state_store

        def refresh(self, symbols, mark_prices):
            del symbols, mark_prices
            return 0

        def get_positions(self):
            return []

    class FakeStrategyService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def generate(self, **kwargs):
            del kwargs
            return []

    class FakeRiskService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter(self, cycle_id, intents, **kwargs):
            del cycle_id, kwargs
            return intents

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            del kwargs
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents
            assert cycle_id is not None
            captured_cycle_ids.append(cycle_id)
            return 0

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: FakeExchange(),
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "AccountingService", FakeAccountingService)
    monkeypatch.setattr(cli, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(cli, "RiskService", FakeRiskService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=True)

    assert cli.run_cycle(settings, force_dry_run=True) == 0
    assert len(captured_cycle_ids) == 1
    assert captured_cycle_ids[0]


def test_run_cycle_kill_switch_blocks_side_effects_but_planning_runs(monkeypatch) -> None:
    events: list[str] = []

    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            del cycle_id

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            events.append("portfolio")
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            events.append("market")
            return {}, _Freshness(max_age_ms=max_age_ms)

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            del kwargs
            events.append("planning")
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            events.append("cancel")
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents, cycle_id
            events.append("execute")
            return 0

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: FakeExchange(),
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=True)

    assert cli.run_cycle(settings, force_dry_run=True) == 0
    assert "planning" in events
    assert "cancel" in events
    assert "execute" in events


def test_stage3_acceptance_run_cycle_dry_run_logs_cycle_completed(monkeypatch, caplog) -> None:
    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            del cycle_id

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            return {"BTC_TRY": 100.0}, _Freshness(max_age_ms=max_age_ms)

    class FakeAccountingService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def refresh(self, symbols, mark_prices):
            del symbols, mark_prices
            return 3

        def get_positions(self):
            return ["btc"]

    class FakeStrategyService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def generate(self, **kwargs):
            del kwargs
            return ["a", "b"]

    class FakeRiskService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter(self, cycle_id: str, intents, **kwargs):
            del cycle_id, kwargs
            return intents[:1]

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            del kwargs
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents, cycle_id
            return 1

    monkeypatch.setattr(
        cli,
        "build_exchange_stage3",
        lambda settings, force_dry_run: FakeExchange(),
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "AccountingService", FakeAccountingService)
    monkeypatch.setattr(cli, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(cli, "RiskService", FakeRiskService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)

    caplog.set_level("INFO")
    settings = Settings(DRY_RUN=True, KILL_SWITCH=False, SYMBOLS="BTC_TRY")

    assert cli.run_cycle(settings, force_dry_run=True) == 0

    cycle_records = [r for r in caplog.records if r.getMessage() == "Cycle completed"]
    assert cycle_records
    payload = getattr(cycle_records[-1], "extra", {})
    assert payload["raw_intents"] == 2
    assert payload["approved_intents"] == 1
    assert payload["orders_submitted"] == 1
    assert payload["fills_inserted"] == 3


def test_run_cycle_stage4_dry_run_writes_stage4_tables(monkeypatch, tmp_path) -> None:
    dry_client = DryRunExchangeClient(
        balances=[Balance(asset="TRY", free=1000.0)],
        orderbooks={"BTCTRY": (1000.0, 1001.0)},
        exchange_info=[
            PairInfo(
                pairSymbol="BTCTRY",
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                tickSize=Decimal("0.1"),
                stepSize=Decimal("0.0001"),
            )
        ],
    )
    dry_client._fills.append(
        TradeFill(
            fill_id="seed-fill-1",
            order_id="seed-order-1",
            symbol="BTCTRY",
            side=OrderSide.BUY,
            price=Decimal("1000"),
            qty=Decimal("0.01"),
            fee=Decimal("0"),
            fee_currency="TRY",
            ts=datetime.now(UTC),
        )
    )

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: DryRunExchangeClientStage4(dry_client),
    )

    db_path = tmp_path / "stage4.sqlite"
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        SYMBOLS="BTC_TRY",
        STATE_DB_PATH=str(db_path),
        FILLS_POLL_LOOKBACK_MINUTES=60,
        MAX_OPEN_ORDERS=5,
        MAX_POSITION_NOTIONAL_TRY=5000,
        MAX_DAILY_LOSS_TRY=1000,
        MAX_DRAWDOWN_PCT=50,
    )

    assert cli.run_cycle_stage4(settings, force_dry_run=True) == 0

    store = cli.StateStore(str(db_path))
    with store._connect() as conn:
        orders = conn.execute("SELECT COUNT(*) AS c FROM stage4_orders").fetchone()["c"]
        fills = conn.execute("SELECT COUNT(*) AS c FROM stage4_fills").fetchone()["c"]
        snapshots = conn.execute("SELECT COUNT(*) AS c FROM pnl_snapshots").fetchone()["c"]
        audits = conn.execute("SELECT COUNT(*) AS c FROM cycle_audit").fetchone()["c"]

    assert orders >= 1
    assert fills >= 1
    assert snapshots >= 1
    assert audits >= 1


def test_stage4_cycle_runner_build_intents_sets_live_mode_flag() -> None:
    runner = Stage4CycleRunner()
    pair_info = [
        PairInfo(
            pairSymbol="BTCTRY",
            numeratorScale=4,
            denominatorScale=2,
            minTotalAmount=Decimal("10"),
        )
    ]
    intents_live, drops_live = runner._build_intents(
        cycle_id="abc123",
        symbols=["BTC_TRY"],
        mark_prices={"BTCTRY": Decimal("100")},
        try_cash=Decimal("100"),
        open_orders=[],
        live_mode=True,
        bootstrap_enabled=True,
        pair_info=pair_info,
        now_utc=datetime.now(UTC),
    )
    intents_dry, drops_dry = runner._build_intents(
        cycle_id="abc123",
        symbols=["BTC_TRY"],
        mark_prices={"BTCTRY": Decimal("100")},
        try_cash=Decimal("100"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=pair_info,
    )

    assert intents_live and intents_dry
    assert drops_live == {}
    assert drops_dry == {}
    assert intents_live[0].mode == "live"
    assert intents_dry[0].mode == "dry_run"
    assert intents_live[0].created_at <= datetime.now(UTC)
    assert intents_dry[0].created_at <= datetime.now(UTC)


def test_stage4_cycle_runner_build_intents_quantized_and_valid_min_notional() -> None:
    runner = Stage4CycleRunner()
    pair = PairInfo(
        pairSymbol="BTCTRY",
        numeratorScale=4,
        denominatorScale=2,
        minTotalAmount=Decimal("10"),
    )
    intents, drops = runner._build_intents(
        cycle_id="abc123",
        symbols=["BTC_TRY"],
        mark_prices={"BTCTRY": Decimal("100.129")},
        try_cash=Decimal("100"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=[pair],
        now_utc=datetime.now(UTC),
    )

    assert drops == {}
    assert len(intents) == 1
    rules = build_exchange_rules(pair)
    expected_price = Quantizer.quantize_price(Decimal("100.129"), rules)
    expected_qty = Quantizer.quantize_qty(Decimal("50") / Decimal("100.129"), rules)
    assert intents[0].price == expected_price
    assert intents[0].qty == expected_qty
    assert Quantizer.validate_min_notional(intents[0].price, intents[0].qty, rules)


def test_stage4_cycle_runner_build_intents_skips_missing_pair_info() -> None:
    runner = Stage4CycleRunner()
    intents, drops = runner._build_intents(
        cycle_id="abc123",
        symbols=["BTC_TRY"],
        mark_prices={"BTCTRY": Decimal("100")},
        try_cash=Decimal("100"),
        open_orders=[],
        live_mode=False,
        bootstrap_enabled=True,
        pair_info=[],
        now_utc=datetime.now(UTC),
    )

    assert intents == []
    assert drops["missing_pair_info"] == 1


def test_run_cycle_stage4_policy_block_records_audit(tmp_path, capsys) -> None:
    settings = Settings(
        DRY_RUN=False,
        KILL_SWITCH=True,
        SAFE_MODE=False,
        STATE_DB_PATH=str(tmp_path / "policy.sqlite"),
        SYMBOLS="BTC_TRY",
    )

    code = cli.run_cycle_stage4(settings, force_dry_run=False)
    out = capsys.readouterr().out
    assert code == 2
    assert out.strip() == "KILL_SWITCH=true blocks side effects"

    store = cli.StateStore(str(tmp_path / "policy.sqlite"))
    with store._connect() as conn:
        sql = (
            "SELECT counts_json, decisions_json, envelope_json "
            "FROM cycle_audit "
            "ORDER BY ts DESC "
            "LIMIT 1"
        )
        row = conn.execute(sql).fetchone()
    assert row is not None
    assert '"blocked_by_policy": 1' in row["counts_json"]
    assert "policy_block:kill_switch" in row["decisions_json"]
    assert row["envelope_json"] is not None


def test_run_cycle_stage4_classifies_capital_invariant_error(monkeypatch, tmp_path, caplog) -> None:
    class FailingRunner:
        def run_one_cycle(self, _settings):
            raise Stage4InvariantError("capital checkpoint mismatch")

    monkeypatch.setattr(cli, "Stage4CycleRunner", lambda: FailingRunner())

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        SAFE_MODE=False,
        STATE_DB_PATH=str(tmp_path / "stage4.sqlite"),
        SYMBOLS="BTC_TRY",
    )

    caplog.set_level("ERROR")
    rc = cli.run_cycle_stage4(settings, force_dry_run=True)

    assert rc == 1
    records = [
        rec
        for rec in caplog.records
        if rec.getMessage() == "Stage 4 cycle failed due to capital/invariant policy"
    ]
    assert records
    payload = getattr(records[-1], "extra", {})
    assert payload["error_category"] == "capital_invariant"


def test_main_stage7_backtest_accepts_dataset_and_out_aliases(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)

    def _fake_run_stage7_backtest(settings, **kwargs):
        del settings
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_stage7_backtest", _fake_run_stage7_backtest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "btcbot",
            "stage7-backtest",
            "--dataset",
            "./data",
            "--out",
            "./out.db",
            "--start",
            "2024-01-01T00:00:00Z",
            "--end",
            "2024-01-01T01:00:00Z",
        ],
    )

    assert cli.main() == 0
    assert captured["data_path"] == "./data"
    assert captured["out_db"] == "./out.db"


def test_main_stage7_backtest_passes_include_adaptation(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)

    def _fake_run_stage7_backtest(settings, **kwargs):
        del settings
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_stage7_backtest", _fake_run_stage7_backtest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "btcbot",
            "stage7-backtest",
            "--dataset",
            "./data",
            "--out",
            "./out.db",
            "--start",
            "2024-01-01T00:00:00Z",
            "--end",
            "2024-01-01T01:00:00Z",
            "--include-adaptation",
        ],
    )

    assert cli.main() == 0
    assert captured["include_adaptation"] is True


def test_main_stage7_run_passes_include_adaptation(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)

    def _fake_run_cycle_stage7(settings, **kwargs):
        del settings
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_cycle_stage7", _fake_run_cycle_stage7)
    monkeypatch.setattr(
        sys,
        "argv",
        ["btcbot", "stage7-run", "--dry-run", "--include-adaptation"],
    )

    assert cli.main() == 0
    assert captured["include_adaptation"] is True


def test_main_run_lock_failure_happens_before_instrumentation(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"
        state_db_path = "./btcbot_state.db"

    called = {"configured": False}

    class _LockFail:
        def __enter__(self):
            raise RuntimeError("LOCKED: test")

        def __exit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return False

    monkeypatch.setattr(cli, "_load_settings", lambda _env_file: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(cli, "single_instance_lock", lambda **_kwargs: _LockFail())

    def _configured(**_kwargs):
        called["configured"] = True

    monkeypatch.setattr(cli, "configure_instrumentation", _configured)
    monkeypatch.setattr(sys, "argv", ["btcbot", "run", "--dry-run", "--once"])

    assert cli.main() == 2
    assert called["configured"] is False


def test_run_stage3_runtime_blocks_second_instance(monkeypatch, tmp_path, capsys) -> None:
    class _Settings:
        state_db_path = str(tmp_path / "state.db")

    started = threading.Event()
    release = threading.Event()
    first_result: dict[str, int] = {}

    def _fake_run_cycle(_settings, force_dry_run: bool = False) -> int:
        del _settings, force_dry_run
        started.set()
        assert release.wait(timeout=5)
        return 0

    monkeypatch.setattr(cli, "run_cycle", _fake_run_cycle)

    def _run_first() -> None:
        first_result["code"] = cli.run_stage3_runtime(
            _Settings(),
            force_dry_run=True,
            loop_enabled=False,
            cycle_seconds=0,
            max_cycles=None,
            jitter_seconds=0,
        )

    first = threading.Thread(target=_run_first)
    first.start()
    assert started.wait(timeout=5)

    second_code = cli.run_stage3_runtime(
        _Settings(),
        force_dry_run=True,
        loop_enabled=False,
        cycle_seconds=0,
        max_cycles=None,
        jitter_seconds=0,
    )

    release.set()
    first.join(timeout=5)

    assert second_code == 2
    assert first_result["code"] == 0
    assert "LOCKED:" in capsys.readouterr().out


def test_run_with_optional_loop_runs_max_cycles() -> None:
    calls = {"count": 0}

    def cycle() -> int:
        calls["count"] += 1
        return 0

    code = cli.run_with_optional_loop(
        command="stage4-run",
        cycle_fn=cycle,
        loop_enabled=True,
        cycle_seconds=0,
        max_cycles=3,
        jitter_seconds=0,
    )
    assert code == 0
    assert calls["count"] == 3


def test_run_with_optional_loop_negative_one_means_infinite_until_interrupt() -> None:
    calls = {"count": 0}

    def cycle() -> int:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise KeyboardInterrupt
        return 0

    code = cli.run_with_optional_loop(
        command="run",
        cycle_fn=cycle,
        loop_enabled=True,
        cycle_seconds=0,
        max_cycles=-1,
        jitter_seconds=0,
    )
    assert code == 0
    assert calls["count"] == 2


def test_main_stage7_run_accepts_db_flag(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)

    def _fake_run_cycle_stage7(settings, **kwargs):
        del settings
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_cycle_stage7", _fake_run_cycle_stage7)
    monkeypatch.setattr(
        sys,
        "argv",
        ["btcbot", "stage7-run", "--dry-run", "--db", "./btcbot_state.db"],
    )

    assert cli.main() == 0
    assert captured["db_path"] == "./btcbot_state.db"


def test_main_stage7_report_accepts_db_flag(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)

    def _fake_run_stage7_report(settings, **kwargs):
        del settings
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_stage7_report", _fake_run_stage7_report)
    monkeypatch.setattr(
        sys,
        "argv",
        ["btcbot", "stage7-report", "--db", "./btcbot_state.db", "--last", "50"],
    )

    assert cli.main() == 0
    assert captured["db_path"] == "./btcbot_state.db"
    assert captured["last"] == 50


def test_main_stage7_db_count_supports_env_fallback(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)

    def _fake_run_stage7_db_count(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_stage7_db_count", _fake_run_stage7_db_count)
    monkeypatch.setenv("STATE_DB_PATH", "./btcbot_state.db")
    monkeypatch.setattr(sys, "argv", ["btcbot", "stage7-db-count"])

    assert cli.main() == 0
    assert captured["db_path"] is None


def test_main_supports_env_file_override(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"
        state_db_path = "btcbot_state.db"

    captured: dict[str, object] = {}

    def _fake_settings(**kwargs):
        captured.update(kwargs)
        return FakeSettings()

    monkeypatch.setattr(cli, "Settings", _fake_settings)
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(cli, "run_cycle", lambda settings, force_dry_run=False: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        ["btcbot", "--env-file", ".env.live", "run", "--once"],
    )

    assert cli.main() == 0
    assert captured["_env_file"] == ".env.live"


def test_main_run_accepts_sleep_seconds_alias(monkeypatch) -> None:
    class FakeSettings:
        log_level = "INFO"
        state_db_path = "btcbot_state.db"

    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "_load_settings", lambda env_file: FakeSettings())
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(cli, "_apply_effective_universe", lambda settings: settings)

    def _fake_loop(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_with_optional_loop", _fake_loop)
    monkeypatch.setattr(sys, "argv", ["btcbot", "run", "--loop", "--sleep-seconds", "10"])

    assert cli.main() == 0
    assert captured["cycle_seconds"] == 10


def test_run_cycle_kill_switch_reports_blocked_not_failed(monkeypatch) -> None:
    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            del cycle_id

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return []

    class FakeMarketDataService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del symbols
            return {}, _Freshness(max_age_ms=max_age_ms)

    class FakeSweepService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def build_order_intents(self, **kwargs):
            del kwargs
            return []

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del cycle_id
            return 0

    class FakeAccountingService:
        def __init__(self, exchange, state_store) -> None:
            del exchange, state_store

        def refresh(self, symbols, mark_prices):
            del symbols, mark_prices
            return 0

        def get_positions(self):
            return []

    class FakeStrategyService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def generate(self, **kwargs):
            del kwargs

            class Intent:
                qty = 1
                limit_price = 100

            return [Intent(), Intent()]

    class FakeRiskService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter(self, **kwargs):
            del kwargs

            class Intent:
                qty = 1
                limit_price = 100

            return [Intent(), Intent()]

    captured_extra: dict[str, object] = {}

    def _capture_info(message, *args, **kwargs):
        if message == "Cycle completed":
            captured_extra.update(kwargs["extra"]["extra"])

    monkeypatch.setattr(
        cli, "build_exchange_stage3", lambda settings, force_dry_run: FakeExchange()
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "SweepService", FakeSweepService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)
    monkeypatch.setattr(cli, "AccountingService", FakeAccountingService)
    monkeypatch.setattr(cli, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(cli, "RiskService", FakeRiskService)
    monkeypatch.setattr(cli.logger, "info", _capture_info)

    settings = Settings(DRY_RUN=True, KILL_SWITCH=True)
    assert cli.run_cycle(settings, force_dry_run=True) == 0
    assert captured_extra["orders_blocked_by_gate"] == 2
    assert captured_extra["orders_suppressed_dry_run"] == 2
    assert captured_extra["orders_failed_exchange"] == 0


def test_format_effective_side_effects_banner_blocked_includes_inputs_and_reasons() -> None:
    inputs = {
        "dry_run": False,
        "kill_switch": True,
        "live_trading_enabled": False,
        "live_trading_ack": False,
    }
    policy = cli.validate_live_side_effects_policy(**inputs)

    banner = cli._format_effective_side_effects_banner(inputs, policy)

    assert "Effective Side-Effects State: BLOCKED" in banner
    assert "reasons=KILL_SWITCH,NOT_ARMED,ACK_MISSING" in banner
    assert "kill_switch=True" in banner
    assert "ack=False" in banner


def test_main_emits_effective_state_banner_once_for_loop_run(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["btcbot", "run", "--loop", "--max-cycles", "2"])
    monkeypatch.setattr(cli, "_load_settings", lambda env_file=None: Settings(DRY_RUN=True))
    monkeypatch.setattr(cli, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "configure_instrumentation", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "_apply_effective_universe", lambda settings: settings)
    monkeypatch.setattr(cli, "run_with_optional_loop", lambda **_kwargs: 0)

    rc = cli.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("Effective Side-Effects State:") == 1


def test_run_cycle_stale_market_data_fail_closed(monkeypatch, caplog) -> None:
    calls: list[str] = []
    audits: list[dict[str, object]] = []

    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            calls.append("state.set_last_cycle_id")
            del cycle_id

        def record_cycle_audit(self, cycle_id: str, counts, decisions, envelope=None) -> None:
            del cycle_id
            audits.append(
                {
                    "counts": counts,
                    "decisions": decisions,
                    "envelope": envelope,
                }
            )

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return []

    class Freshness:
        is_stale = True
        observed_age_ms = 20_000
        max_age_ms = 5_000
        source_mode = "ws"
        connected = False
        missing_symbols = ("BTC_TRY",)

    class FakeMarketDataService:
        def __init__(self, exchange, **kwargs) -> None:
            del exchange, kwargs

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del max_age_ms
            return {symbol: 100.0 for symbol in symbols}, Freshness()

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def cancel_stale_orders(self, cycle_id: str) -> int:
            calls.append("execution.cancel_stale")
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            calls.append("execution.execute")
            del intents, cycle_id
            return 0

    class FakeStrategyService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def generate(self, **kwargs):
            calls.append("strategy.generate")
            del kwargs
            return []

    class FakeRiskService:
        def __init__(self, risk_policy, state_store) -> None:
            del risk_policy, state_store

        def filter(self, cycle_id: str, intents, **kwargs):
            calls.append("risk.filter")
            del cycle_id, kwargs
            return intents

    monkeypatch.setattr(
        cli, "build_exchange_stage3", lambda settings, force_dry_run: FakeExchange()
    )
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)
    monkeypatch.setattr(cli, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(cli, "RiskService", FakeRiskService)

    caplog.set_level("INFO", logger="btcbot.cli")
    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, SAFE_MODE=False, MAX_MARKET_DATA_AGE_MS=5000
    )
    assert cli.run_cycle(settings, force_dry_run=True) == 0

    assert "strategy.generate" not in calls
    assert "risk.filter" not in calls
    assert "execution.execute" not in calls
    assert "execution.cancel_stale" not in calls

    decision_events = [
        record for record in caplog.records if record.getMessage() == "decision_event"
    ]
    assert decision_events
    payload = json.loads(JsonFormatter().format(decision_events[-1]))
    assert payload["decision_layer"] == "market_data"
    assert payload["reason_code"] == "market_data:stale"
    assert payload["action"] == "BLOCK"
    assert payload["scope"] == "global"
    assert payload["observed_age_ms"] == 20000
    assert payload["max_age_ms"] == 5000
    assert payload["missing_symbols"] == ["BTC_TRY"]

    assert audits
    assert audits[-1]["decisions"] == ["market_data:stale"]


def test_run_cycle_uses_single_cash_snapshot_for_risk_and_summary(monkeypatch, caplog) -> None:
    import btcbot.cli as cli

    class FakeExchange:
        def close(self) -> None:
            return None

    class FakeStateStore:
        def __init__(self, db_path: str) -> None:
            del db_path

        def set_last_cycle_id(self, cycle_id: str) -> None:
            del cycle_id

        def find_open_or_unknown_orders(self):
            return []

        def get_last_intent_ts_by_symbol_side(self):
            return {}

        def record_intent(self, intent, now) -> None:
            del intent, now

        def record_action(self, cycle_id: str, action_type: str, payload_hash: str):
            del cycle_id, action_type
            return payload_hash

    class FakePortfolioService:
        def __init__(self, exchange) -> None:
            del exchange

        def get_balances(self):
            return [
                Balance(asset="TRY", free=1306.0, locked=0.0),
                Balance(asset="BTC", free=0.0, locked=0.0),
            ]

    class Freshness:
        is_stale = False

    class FakeMarketDataService:
        def __init__(self, exchange, **kwargs) -> None:
            del exchange, kwargs

        def get_best_bids_with_freshness(self, symbols, *, max_age_ms: int):
            del max_age_ms
            return {symbol: 100.0 for symbol in symbols}, Freshness()

    class FakeExecutionService:
        def __init__(self, **kwargs) -> None:
            del kwargs
            self.kill_switch = False

        def cancel_stale_orders(self, cycle_id: str) -> int:
            del cycle_id
            return 0

        def execute_intents(self, intents, cycle_id: str | None = None) -> int:
            del intents, cycle_id
            return 0

    class FakeAccountingService:
        def __init__(self, exchange, state_store) -> None:
            del exchange, state_store

        def refresh(self, symbols, mark_prices):
            del symbols, mark_prices
            return 0

        def get_positions(self):
            return []

    class FakeStrategyService:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def generate(self, **kwargs):
            cycle_id = kwargs["cycle_id"]
            return [
                Intent.create(
                    cycle_id=cycle_id,
                    symbol="BTCTRY",
                    side=OrderSide.BUY,
                    qty=Decimal("20"),
                    limit_price=Decimal("100"),
                    reason="force cash reserve block",
                )
            ]

    class FakeStartupRecoveryService:
        def run(self, **kwargs):
            del kwargs

            class Result:
                observe_only_required = False
                invariant_errors: list[str] = []
                observe_only_reason: str | None = None

            return Result()

    class StaticRulesProvider:
        def __init__(self, market_data_service) -> None:
            del market_data_service

        def get_rules(self, symbol: str):
            del symbol
            return ExchangeRules(
                min_notional=Decimal("0"),
                price_tick=Decimal("0.01"),
                qty_step=Decimal("0.0001"),
            )

    monkeypatch.setattr(cli, "build_exchange_stage3", lambda settings, force_dry_run: FakeExchange())
    monkeypatch.setattr(cli, "StateStore", FakeStateStore)
    monkeypatch.setattr(cli, "PortfolioService", FakePortfolioService)
    monkeypatch.setattr(cli, "MarketDataService", FakeMarketDataService)
    monkeypatch.setattr(cli, "ExecutionService", FakeExecutionService)
    monkeypatch.setattr(cli, "AccountingService", FakeAccountingService)
    monkeypatch.setattr(cli, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(cli, "StartupRecoveryService", FakeStartupRecoveryService)
    monkeypatch.setattr(cli, "MarketDataExchangeRulesProvider", StaticRulesProvider)

    caplog.set_level("INFO")
    settings = Settings(
        DRY_RUN=True,
        SAFE_MODE=False,
        KILL_SWITCH=False,
        TRY_CASH_TARGET="300",
        SYMBOLS='["BTCTRY"]',
        MAX_ORDERS_PER_CYCLE=1,
    )

    assert cli.run_cycle(settings, force_dry_run=True) == 0

    decision_payloads = [
        json.loads(JsonFormatter().format(record))
        for record in caplog.records
        if record.getMessage() == "decision_event"
    ]
    reserve_blocks = [
        payload
        for payload in decision_payloads
        if payload.get("reason_code") == "risk_block:cash_reserve_target"
    ]
    assert reserve_blocks

    completed_payloads = [
        json.loads(JsonFormatter().format(record))
        for record in caplog.records
        if record.getMessage() == "Cycle completed"
    ]
    assert completed_payloads

    reserve_payload = reserve_blocks[-1]
    cycle_payload = completed_payloads[-1]
    assert reserve_payload["cash_try_free"] == cycle_payload["cash_try_free"]
    assert reserve_payload["try_cash_target"] == cycle_payload["try_cash_target"]
    assert reserve_payload["investable_try"] == cycle_payload["investable_try"]
    assert cycle_payload["cash_try_free"] == "1306.0"
    assert cycle_payload["investable_try"] == "1006.0"
