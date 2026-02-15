from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from btcbot import cli
from btcbot.config import Settings
from btcbot.domain.adaptation_models import ParamChange
from btcbot.domain.ledger import LedgerEvent, LedgerEventType
from btcbot.domain.risk_budget import Mode
from btcbot.services.state_store import StateStore


def _selected_btc_universe(*args, **kwargs):
    del args, kwargs
    return SimpleNamespace(
        selected_symbols=["BTCTRY"],
        scored=[
            SimpleNamespace(
                symbol="BTCTRY",
                total_score=Decimal("1"),
                breakdown={"liquidity": "1"},
            )
        ],
    )


def test_stage7_run_dry_run_persists_trace_and_metrics(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )

    class _Pair:
        def __init__(self, pair_symbol: str) -> None:
            self.pair_symbol = pair_symbol
            self.tick_size = Decimal("0.1")
            self.step_size = Decimal("0.0001")
            self.min_total_amount = Decimal("10")

    class _Exchange:
        def get_exchange_info(self):
            return [_Pair("BTC_TRY"), _Pair("ETH_TRY")]

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "100",
                    "high": "101",
                    "low": "99",
                },
                {"pairSymbol": "ETH_TRY", "volume": "900", "last": "50", "high": "51", "low": "49"},
            ]

        def get_orderbook(self, symbol):
            if symbol == "BTCTRY":
                return Decimal("99"), Decimal("100")
            return Decimal("49"), Decimal("50")

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "100"} for _ in range(lookback)]

        def submit_order(self, *args, **kwargs):
            raise AssertionError("live submit should never be called in stage7 dry-run")

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.adaptation_service.AdaptationService.evaluate_and_apply",
        lambda self, **kwargs: ParamChange(
            change_id="s7chg:test:1->1:abc",
            ts=datetime(2024, 1, 1, tzinfo=UTC),
            from_version=1,
            to_version=1,
            changes={},
            reason="test",
            metrics_window={"cycles": "1"},
            outcome="REJECTED",
            notes=["test"],
        ),
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute("SELECT * FROM stage7_cycle_trace").fetchone()
        metrics = conn.execute("SELECT * FROM stage7_ledger_metrics").fetchone()
        run_metrics = conn.execute("SELECT * FROM stage7_run_metrics").fetchone()
        active_params = conn.execute(
            "SELECT * FROM stage7_params_active WHERE key = 'active'"
        ).fetchone()
        intents = conn.execute("SELECT * FROM stage7_order_intents").fetchall()
        oms_orders = conn.execute("SELECT * FROM stage7_orders").fetchall()
        oms_events = conn.execute("SELECT * FROM stage7_order_events").fetchall()
    finally:
        conn.close()

    assert cycle is not None
    assert metrics is not None
    assert run_metrics is not None
    assert active_params is not None
    selected_universe = json.loads(str(cycle["selected_universe_json"]))
    assert selected_universe
    universe_scores = json.loads(str(cycle["universe_scores_json"]))
    assert universe_scores
    mode_payload = json.loads(str(cycle["mode_json"]))
    portfolio_plan = json.loads(str(cycle["portfolio_plan_json"]))
    order = {"NORMAL": 0, "REDUCE_RISK_ONLY": 1, "OBSERVE_ONLY": 2}
    assert order[mode_payload["final_mode"]] >= order[mode_payload["base_mode"]]
    assert portfolio_plan
    assert "allocations" in portfolio_plan
    assert "actions" in portfolio_plan
    trace_summary = json.loads(str(cycle["intents_summary_json"]))
    param_change = json.loads(str(cycle["param_change_json"]))
    assert int(cycle["active_param_version"]) == int(active_params["version"])
    assert isinstance(param_change, dict)
    assert trace_summary["order_intents_total"] >= 1
    assert "order_intents_planned" in trace_summary
    assert "order_intents_skipped" in trace_summary
    assert "rules_stats" in trace_summary
    assert "oms_summary" in trace_summary
    assert intents
    assert oms_orders
    assert oms_events
    assert int(run_metrics["latency_ms_total"]) >= 0
    assert int(run_metrics["selection_ms"]) >= 0
    assert int(run_metrics["planning_ms"]) >= 0
    alerts = json.loads(str(run_metrics["alert_flags_json"]))
    assert "drawdown_breach" in alerts
    assert "reject_spike" in alerts


def test_stage7_run_respects_reduce_risk_mode(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_reduce.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Pair:
        def __init__(self, pair_symbol: str) -> None:
            self.pair_symbol = pair_symbol

    class _Exchange:
        def get_exchange_info(self):
            return [_Pair("BTC_TRY")]

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "101",
                    "high": "102",
                    "low": "100",
                }
            ]

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "101"} for _ in range(lookback)]

        def get_orderbook(self, symbol):
            del symbol
            return Decimal("100"), Decimal("102")

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_btc_universe,
    )
    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.get_latest_risk_mode",
        lambda self: Mode.REDUCE_RISK_ONLY,
    )
    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.list_stage4_open_orders",
        lambda self: [
            SimpleNamespace(
                status="simulated_submitted",
                symbol="BTC_TRY",
                side="BUY",
                price=Decimal("101"),
                qty=Decimal("1"),
                client_order_id="b1",
                exchange_order_id="e1",
            ),
            SimpleNamespace(
                status="simulated_submitted",
                symbol="BTC_TRY",
                side="SELL",
                price=Decimal("101"),
                qty=Decimal("1"),
                client_order_id="s1",
                exchange_order_id="e2",
            ),
        ],
    )

    store = StateStore(db_path=str(db_path))
    store.append_ledger_events(
        [
            LedgerEvent(
                event_id="seed-buy",
                ts=datetime(2024, 1, 1, tzinfo=UTC),
                symbol="BTCTRY",
                type=LedgerEventType.FILL,
                side="BUY",
                qty=Decimal("1"),
                price=Decimal("100"),
                fee=None,
                fee_currency=None,
                exchange_trade_id="seed-buy",
                exchange_order_id="seed-order",
                client_order_id="seed-client",
                meta={"source": "test"},
            )
        ]
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE7_RULES_REQUIRE_METADATA=False,
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute("SELECT * FROM stage7_cycle_trace").fetchone()
    finally:
        conn.close()

    assert cycle is not None
    mode_payload = json.loads(str(cycle["mode_json"]))
    decisions = json.loads(str(cycle["order_decisions_json"]))
    portfolio_plan = json.loads(str(cycle["portfolio_plan_json"]))
    order = {"NORMAL": 0, "REDUCE_RISK_ONLY": 1, "OBSERVE_ONLY": 2}
    assert mode_payload["base_mode"] == "REDUCE_RISK_ONLY"
    assert order[mode_payload["final_mode"]] >= order[mode_payload["base_mode"]]
    assert any(d.get("status") == "submitted" and d.get("side") == "SELL" for d in decisions)
    assert any(d.get("status") == "skipped" for d in decisions)
    assert portfolio_plan
    assert all(action.get("side") == "SELL" for action in portfolio_plan.get("actions", []))


def test_stage7_run_skips_open_order_with_missing_mark_price(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_missing_mark.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Pair:
        def __init__(self, pair_symbol: str) -> None:
            self.pair_symbol = pair_symbol

    class _Exchange:
        def get_exchange_info(self):
            return [_Pair("BTC_TRY")]

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "101",
                    "high": "102",
                    "low": "100",
                }
            ]

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "101"} for _ in range(lookback)]

        def get_orderbook(self, symbol):
            if symbol == "BTCTRY":
                return Decimal("100"), Decimal("102")
            raise RuntimeError("orderbook unavailable")

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_btc_universe,
    )
    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.list_stage4_open_orders",
        lambda self: [
            SimpleNamespace(
                status="simulated_submitted",
                symbol="XRP_TRY",
                side="BUY",
                price=Decimal("20"),
                qty=Decimal("1"),
                client_order_id="x1",
                exchange_order_id="e-x1",
            )
        ],
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE7_RULES_REQUIRE_METADATA=False,
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute("SELECT * FROM stage7_cycle_trace").fetchone()
    finally:
        conn.close()

    assert cycle is not None
    decisions = json.loads(str(cycle["order_decisions_json"]))
    assert any(
        decision.get("status") == "skipped" and decision.get("reason") == "missing_mark_price"
        for decision in decisions
    )


def test_stage7_universe_selection_does_not_change_ledger_metrics_shape(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "stage7_ledger_shape.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
    )
    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        metrics = conn.execute("SELECT * FROM stage7_ledger_metrics").fetchone()
        run_metrics = conn.execute("SELECT * FROM stage7_run_metrics").fetchone()
        active_params = conn.execute(
            "SELECT * FROM stage7_params_active WHERE key = 'active'"
        ).fetchone()
    finally:
        conn.close()

    assert metrics is not None
    assert run_metrics is not None
    assert active_params is not None
    for key in [
        "gross_pnl_try",
        "realized_pnl_try",
        "unrealized_pnl_try",
        "net_pnl_try",
        "fees_try",
        "slippage_try",
        "turnover_try",
        "equity_try",
        "max_drawdown",
    ]:
        assert key in metrics.keys()


def test_stage7_policy_skip_symbol(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_skip_symbol.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Pair:
        def __init__(self, pair_symbol: str) -> None:
            self.pair_symbol = pair_symbol

    class _Exchange:
        def get_exchange_info(self):
            return [_Pair("BTC_TRY")]

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "100",
                    "high": "101",
                    "low": "99",
                }
            ]

        def get_orderbook(self, symbol):
            if symbol == "BTCTRY":
                return Decimal("99"), Decimal("100")
            if symbol == "XRPTRY":
                return Decimal("19"), Decimal("20")
            raise RuntimeError("unknown symbol")

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "100"} for _ in range(lookback)]

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_btc_universe,
    )

    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.list_stage4_open_orders",
        lambda self: [
            SimpleNamespace(
                status="simulated_submitted",
                symbol="XRP_TRY",
                side="SELL",
                price=Decimal("20"),
                qty=Decimal("1"),
                client_order_id="xrp1",
                exchange_order_id="xrp-ex-1",
            )
        ],
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE7_RULES_REQUIRE_METADATA=True,
        STAGE7_RULES_INVALID_METADATA_POLICY="skip_symbol",
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        intents = conn.execute("SELECT intent_json FROM stage7_order_intents").fetchall()
        cycle = conn.execute("SELECT intents_summary_json FROM stage7_cycle_trace").fetchone()
    finally:
        conn.close()

    assert intents
    parsed = [json.loads(str(row["intent_json"])) for row in intents]
    assert any(str(i.get("skip_reason", "")).startswith("rules_unavailable:") for i in parsed)
    summary = json.loads(str(cycle["intents_summary_json"]))
    assert (
        summary["rules_stats"]["rules_missing_count"]
        + summary["rules_stats"].get(
            "rules_invalid_metadata_count", summary["rules_stats"].get("rules_invalid_count", 0)
        )
    ) >= 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle_full = conn.execute("SELECT order_decisions_json FROM stage7_cycle_trace").fetchone()
    finally:
        conn.close()
    decisions = json.loads(str(cycle_full["order_decisions_json"]))
    assert any(decision.get("status") == "skipped" for decision in decisions)


def test_stage7_policy_observe_only_cycle(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_observe_cycle.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Pair:
        def __init__(self, pair_symbol: str) -> None:
            self.pair_symbol = pair_symbol

    class _Exchange:
        def get_exchange_info(self):
            return [_Pair("BTC_TRY")]

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "100",
                    "high": "101",
                    "low": "99",
                }
            ]

        def get_orderbook(self, symbol):
            if symbol == "BTCTRY":
                return Decimal("99"), Decimal("100")
            if symbol == "XRPTRY":
                return Decimal("19"), Decimal("20")
            raise RuntimeError("unknown symbol")

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "100"} for _ in range(lookback)]

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_btc_universe,
    )
    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.list_stage4_open_orders",
        lambda self: [
            SimpleNamespace(
                status="simulated_submitted",
                symbol="XRP_TRY",
                side="SELL",
                price=Decimal("20"),
                qty=Decimal("1"),
                client_order_id="xrp2",
                exchange_order_id="xrp-ex-2",
            )
        ],
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE7_RULES_REQUIRE_METADATA=True,
        STAGE7_RULES_INVALID_METADATA_POLICY="observe_only_cycle",
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute(
            "SELECT mode_json, intents_summary_json FROM stage7_cycle_trace"
        ).fetchone()
        intents_count = conn.execute("SELECT COUNT(*) FROM stage7_order_intents").fetchone()[0]
    finally:
        conn.close()

    mode_payload = json.loads(str(cycle["mode_json"]))
    assert mode_payload["final_mode"] == "OBSERVE_ONLY"
    assert intents_count == 0
    summary = json.loads(str(cycle["intents_summary_json"]))
    assert (
        summary["rules_stats"]["rules_missing_count"]
        + summary["rules_stats"].get(
            "rules_invalid_metadata_count", summary["rules_stats"].get("rules_invalid_count", 0)
        )
    ) >= 1


def test_intents_summary_counts_correct(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_counts.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Pair:
        def __init__(self, pair_symbol: str) -> None:
            self.pair_symbol = pair_symbol

    class _Exchange:
        def get_exchange_info(self):
            return [_Pair("BTC_TRY")]

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "100",
                    "high": "101",
                    "low": "99",
                }
            ]

        def get_orderbook(self, symbol):
            del symbol
            return Decimal("99"), Decimal("100")

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "100"} for _ in range(lookback)]

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_btc_universe,
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute("SELECT intents_summary_json FROM stage7_cycle_trace").fetchone()
    finally:
        conn.close()

    summary = json.loads(str(cycle["intents_summary_json"]))
    assert summary["order_intents_total"] == (
        summary["order_intents_planned"] + summary["order_intents_skipped"]
    )


def test_stage7_lifecycle_symbols_included_in_rules_coverage(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_lifecycle_symbols.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Pair:
        def __init__(self, pair_symbol: str) -> None:
            self.pair_symbol = pair_symbol

    class _Exchange:
        def get_exchange_info(self):
            return [_Pair("BTC_TRY")]

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "100",
                    "high": "101",
                    "low": "99",
                }
            ]

        def get_orderbook(self, symbol):
            if symbol == "BTCTRY":
                return Decimal("99"), Decimal("100")
            if symbol == "XRPTRY":
                return Decimal("19"), Decimal("20")
            raise RuntimeError("unknown symbol")

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "100"} for _ in range(lookback)]

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_btc_universe,
    )
    monkeypatch.setattr(
        "btcbot.services.state_store.StateStore.list_stage4_open_orders",
        lambda self: [
            SimpleNamespace(
                status="simulated_submitted",
                symbol="XRP_TRY",
                side="SELL",
                price=Decimal("20"),
                qty=Decimal("1"),
                client_order_id="xrp-lifecycle-1",
                exchange_order_id="xrp-ex-lifecycle-1",
            )
        ],
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE7_RULES_REQUIRE_METADATA=True,
        STAGE7_RULES_INVALID_METADATA_POLICY="skip_symbol",
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute(
            """
            SELECT intents_summary_json, order_decisions_json, order_intents_json
            FROM stage7_cycle_trace
            """
        ).fetchone()
    finally:
        conn.close()

    summary = json.loads(str(cycle["intents_summary_json"]))
    assert (
        summary["rules_stats"]["rules_missing_count"]
        + summary["rules_stats"].get(
            "rules_invalid_metadata_count", summary["rules_stats"].get("rules_invalid_count", 0)
        )
    ) >= 1

    decisions = json.loads(str(cycle["order_decisions_json"]))
    traces = json.loads(str(cycle["order_intents_json"]))
    assert any(
        decision.get("symbol") == "XRPTRY"
        and decision.get("status") == "skipped"
        and str(decision.get("reason", "")).startswith("rules_unavailable:")
        for decision in decisions
    ) or any(
        trace.get("symbol") == "XRPTRY"
        and str(trace.get("skip_reason", "")).startswith("rules_unavailable:")
        for trace in traces
    )


def test_stage7_policy_observe_only_on_exchange_rules_error(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "stage7_observe_rules_error.db"

    def _fake_stage4(self, settings):
        del self, settings
        return 0

    class _Exchange:
        def get_exchange_info(self):
            raise TimeoutError("temporary_exchangeinfo_outage")

        def get_ticker_stats(self):
            return [
                {
                    "pairSymbol": "BTC_TRY",
                    "volume": "1000",
                    "last": "100",
                    "high": "101",
                    "low": "99",
                }
            ]

        def get_orderbook(self, symbol):
            del symbol
            return Decimal("99"), Decimal("100")

        def get_candles(self, symbol, lookback):
            del symbol
            return [{"close": "100"} for _ in range(lookback)]

        def close(self):
            return None

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.Stage4CycleRunner.run_one_cycle", _fake_stage4
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: SimpleNamespace(client=_Exchange(), close=lambda: None),
    )
    monkeypatch.setattr(
        "btcbot.services.stage7_cycle_runner.UniverseSelectionService.select_universe",
        _selected_btc_universe,
    )

    settings = Settings(
        DRY_RUN=True,
        STAGE7_ENABLED=True,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
        STAGE7_RULES_REQUIRE_METADATA=True,
        STAGE7_RULES_INVALID_METADATA_POLICY="observe_only_cycle",
    )

    assert cli.run_cycle_stage7(settings, force_dry_run=True) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cycle = conn.execute(
            "SELECT mode_json, intents_summary_json FROM stage7_cycle_trace"
        ).fetchone()
    finally:
        conn.close()

    mode_payload = json.loads(str(cycle["mode_json"]))
    summary = json.loads(str(cycle["intents_summary_json"]))
    assert mode_payload["final_mode"] == "OBSERVE_ONLY"
    assert summary["rules_stats"]["rules_error_count"] >= 1
