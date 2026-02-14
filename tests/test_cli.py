from __future__ import annotations

import sys
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
from btcbot.domain.models import Balance, OrderSide, PairInfo
from btcbot.domain.stage4 import Quantizer
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner


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


def test_health_returns_zero_on_success(monkeypatch) -> None:
    monkeypatch.setattr(cli, "BtcturkHttpClient", HealthyClient)
    settings = Settings()

    assert cli.run_health(settings) == 0


def test_health_returns_nonzero_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(cli, "BtcturkHttpClient", UnhealthyClient)
    settings = Settings()

    assert cli.run_health(settings) == 1


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
    settings = Settings(DRY_RUN=False, KILL_SWITCH=True)

    code = cli.run_cycle(settings, force_dry_run=False)

    out = capsys.readouterr().out
    assert code == 2
    assert out.strip() == "KILL_SWITCH=true blocks side effects"


def test_run_cycle_live_not_armed_message_is_specific(capsys) -> None:
    settings = Settings(DRY_RUN=False, KILL_SWITCH=False, LIVE_TRADING=False)

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

        def get_best_bids(self, symbols):
            del symbols
            events.append("market.get_best_bids")
            return {}

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

        def get_best_bids(self, symbols):
            del symbols
            return {}

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

        def get_best_bids(self, symbols):
            del symbols
            return {"btc_try": 100.0}

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

        def filter(self, cycle_id, intents):
            del cycle_id
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

        def get_best_bids(self, symbols):
            del symbols
            return {}

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

        def filter(self, cycle_id, intents):
            del cycle_id
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

        def get_best_bids(self, symbols):
            del symbols
            events.append("market")
            return {}

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

        def get_best_bids(self, symbols):
            del symbols
            return {"BTC_TRY": 100.0}

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

        def filter(self, cycle_id: str, intents):
            del cycle_id
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
    assert payload["orders"] == 1
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
    )

    assert intents == []
    assert drops["missing_pair_info"] == 1


def test_run_cycle_stage4_policy_block_records_audit(tmp_path, capsys) -> None:
    settings = Settings(
        DRY_RUN=False,
        KILL_SWITCH=True,
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
