from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.anomalies import AnomalyCode, AnomalyEvent, combine_modes, decide_degrade
from btcbot.domain.risk_budget import Mode
from btcbot.domain.stage4 import LifecycleAction, LifecycleActionType, PnLSnapshot
from btcbot.services import stage4_cycle_runner as runner_module
from btcbot.services.anomaly_detector_service import AnomalyDetectorConfig, AnomalyDetectorService
from btcbot.services.ledger_service import PnlReport
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.state_store import StateStore


def test_decide_degrade_cooldown_active_keeps_override_and_reasons() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    decision = decide_degrade(
        anomalies=[],
        now=now,
        current_override=Mode.REDUCE_RISK_ONLY,
        cooldown_until=now + timedelta(minutes=5),
        last_reasons=["ORDER_REJECT_SPIKE"],
        recent_warn_count=999,
        warn_threshold=3,
        warn_codes={AnomalyCode.ORDER_REJECT_SPIKE},
    )
    assert decision.mode_override == Mode.REDUCE_RISK_ONLY
    assert decision.cooldown_until == now + timedelta(minutes=5)
    assert decision.reasons == ["ORDER_REJECT_SPIKE"]


def test_decide_degrade_error_forces_observe_only() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    decision = decide_degrade(
        anomalies=[
            AnomalyEvent(
                code=AnomalyCode.PNL_DIVERGENCE,
                severity="ERROR",
                ts=now,
                details={},
            )
        ],
        now=now,
        current_override=None,
        cooldown_until=None,
        last_reasons=None,
        recent_warn_count=0,
        warn_threshold=3,
        warn_codes={AnomalyCode.PNL_DIVERGENCE},
    )
    assert decision.mode_override == Mode.OBSERVE_ONLY
    assert decision.cooldown_until == now + timedelta(minutes=30)
    assert decision.reasons == [AnomalyCode.PNL_DIVERGENCE.value]


def test_decide_degrade_warn_threshold_sets_reduce_risk() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    anomalies = [
        AnomalyEvent(code=AnomalyCode.STALE_MARKET_DATA, severity="WARN", ts=now, details={})
    ]
    decision = decide_degrade(
        anomalies=anomalies,
        now=now,
        current_override=None,
        cooldown_until=None,
        last_reasons=None,
        recent_warn_count=3,
        warn_threshold=3,
        warn_codes={AnomalyCode.STALE_MARKET_DATA},
    )
    assert decision.mode_override == Mode.REDUCE_RISK_ONLY
    assert decision.cooldown_until == now + timedelta(minutes=15)


def test_decide_degrade_none_returns_clear_state() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    decision = decide_degrade(
        anomalies=[],
        now=now,
        current_override=None,
        cooldown_until=None,
        last_reasons=None,
        recent_warn_count=0,
        warn_threshold=3,
        warn_codes={AnomalyCode.ORDER_REJECT_SPIKE},
    )
    assert decision.mode_override is None
    assert decision.cooldown_until is None
    assert decision.reasons == []


def test_detector_stale_and_reject_and_pnl_divergence() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    detector = AnomalyDetectorService(
        config=AnomalyDetectorConfig(
            stale_market_data_seconds=30,
            reject_spike_threshold=3,
            latency_spike_ms=2000,
            cursor_stall_cycles=5,
            clock_skew_seconds_threshold=30,
            pnl_divergence_try_warn=Decimal("50"),
            pnl_divergence_try_error=Decimal("200"),
        ),
        now_provider=lambda: now,
    )
    snapshot = PnLSnapshot(
        total_equity_try=Decimal("1300"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now,
        realized_total_try=Decimal("0"),
    )
    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("0")},
        per_symbol=[],
        equity_estimate=Decimal("1000"),
    )

    events = detector.detect(
        market_data_age_seconds={"BTCTRY": 45},
        reject_count=3,
        cycle_duration_ms=2100,
        cursor_stall_by_symbol={"BTCTRY": 5},
        pnl_snapshot=snapshot,
        pnl_report=pnl_report,
    )
    codes = {event.code for event in events}
    assert AnomalyCode.STALE_MARKET_DATA in codes
    assert AnomalyCode.ORDER_REJECT_SPIKE in codes
    assert AnomalyCode.PNL_DIVERGENCE in codes


def test_detector_skips_stale_when_market_timestamps_missing() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    detector = AnomalyDetectorService(now_provider=lambda: now)
    snapshot = PnLSnapshot(
        total_equity_try=Decimal("1000"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now,
        realized_total_try=Decimal("0"),
    )
    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("0")},
        per_symbol=[],
        equity_estimate=Decimal("1000"),
    )

    events = detector.detect(
        market_data_age_seconds=None,
        reject_count=0,
        cycle_duration_ms=None,
        cursor_stall_by_symbol={},
        pnl_snapshot=snapshot,
        pnl_report=pnl_report,
    )
    assert all(event.code != AnomalyCode.STALE_MARKET_DATA for event in events)


def test_detector_pnl_divergence_warn_vs_error() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    detector = AnomalyDetectorService(now_provider=lambda: now)
    snapshot = PnLSnapshot(
        total_equity_try=Decimal("1100"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now,
        realized_total_try=Decimal("0"),
    )
    warn_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("0")},
        per_symbol=[],
        equity_estimate=Decimal("1040"),
    )
    error_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("0")},
        per_symbol=[],
        equity_estimate=Decimal("800"),
    )

    warn_event = [
        event
        for event in detector.detect(
            market_data_age_seconds={},
            reject_count=0,
            cycle_duration_ms=None,
            cursor_stall_by_symbol={},
            pnl_snapshot=snapshot,
            pnl_report=warn_report,
        )
        if event.code == AnomalyCode.PNL_DIVERGENCE
    ][0]
    error_event = [
        event
        for event in detector.detect(
            market_data_age_seconds={},
            reject_count=0,
            cycle_duration_ms=None,
            cursor_stall_by_symbol={},
            pnl_snapshot=snapshot,
            pnl_report=error_report,
        )
        if event.code == AnomalyCode.PNL_DIVERGENCE
    ][0]
    assert warn_event.severity == "WARN"
    assert error_event.severity == "ERROR"


def test_runner_reject_and_cursor_stall_anomalies_use_real_inputs(monkeypatch, tmp_path) -> None:
    class FakeExchange:
        def get_orderbook(self, symbol: str):
            del symbol
            return (100.0, 101.0)

        def get_balances(self):
            return [type("B", (), {"asset": "TRY", "free": Decimal("1000")})()]

        def list_open_orders(self, symbol: str):
            del symbol
            return []

        def get_recent_fills(self, symbol: str, since_ms: int | None = None):
            del symbol, since_ms
            return []

        def get_exchange_info(self):
            return []

        def close(self):
            return

    exchange = FakeExchange()
    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    class FakeLifecycle:
        def __init__(self, stale_after_sec: int) -> None:
            del stale_after_sec

        def plan(self, intents, current_open_orders, mid_price):
            del intents, current_open_orders, mid_price
            actions = [
                LifecycleAction(
                    action_type=LifecycleActionType.SUBMIT,
                    symbol="BTCTRY",
                    side="BUY",
                    price=Decimal("100"),
                    qty=Decimal("0.1"),
                    reason="test",
                    client_order_id="buy-1",
                )
            ]
            return type("P", (), {"actions": actions, "audit_reasons": []})()

    class FakeRiskPolicy:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def filter_actions(self, actions, **kwargs):
            del kwargs
            return actions, []

    class FakeExecution:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def execute_with_report(self, actions):
            del actions
            return type(
                "ER",
                (),
                {
                    "executed_total": 0,
                    "submitted": 0,
                    "canceled": 0,
                    "simulated": 0,
                    "rejected": 3,
                },
            )()

    class FakeRiskBudgetService:
        def __init__(self, state_store) -> None:
            del state_store

        def compute_decision(self, **kwargs):
            del kwargs
            decision = type(
                "D",
                (),
                {
                    "mode": Mode.NORMAL,
                    "reasons": ["OK"],
                    "signals": type(
                        "S",
                        (),
                        {
                            "drawdown_try": Decimal("0"),
                            "gross_exposure_try": Decimal("0"),
                            "fees_try_today": Decimal("0"),
                        },
                    )(),
                },
            )()
            now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            return decision, None, Decimal("0"), Decimal("0"), now.date()

        def persist_decision(self, **kwargs):
            del kwargs
            return

    monkeypatch.setattr(runner_module, "OrderLifecycleService", FakeLifecycle)
    monkeypatch.setattr(runner_module, "RiskPolicy", FakeRiskPolicy)
    monkeypatch.setattr(runner_module, "ExecutionService", FakeExecution)
    monkeypatch.setattr(runner_module, "RiskBudgetService", FakeRiskBudgetService)

    db_path = tmp_path / "stage6_4_1.sqlite"
    store = StateStore(str(db_path))
    store.upsert_degrade_state_current(
        cooldown_until=None,
        current_override_mode=None,
        last_reasons_json="[]",
        warn_window_count=0,
        last_warn_codes_json="[]",
        cursor_stall_cycles_json=json.dumps({"BTCTRY": 4}),
        last_reject_count=0,
    )

    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(db_path),
        CURSOR_STALL_CYCLES=5,
        REJECT_SPIKE_THRESHOLD=3,
    )
    runner = Stage4CycleRunner()
    assert runner.run_one_cycle(settings) == 0

    with store._connect() as conn:
        rows = conn.execute("SELECT code FROM anomaly_events").fetchall()
    codes = {str(row["code"]) for row in rows}
    assert AnomalyCode.ORDER_REJECT_SPIKE.value in codes
    assert AnomalyCode.CURSOR_STALL.value in codes
    assert AnomalyCode.STALE_MARKET_DATA.value not in codes


def test_combine_modes_is_monotonic() -> None:
    assert combine_modes(Mode.NORMAL, Mode.REDUCE_RISK_ONLY) == Mode.REDUCE_RISK_ONLY
    assert combine_modes(Mode.REDUCE_RISK_ONLY, Mode.NORMAL) == Mode.REDUCE_RISK_ONLY
    assert combine_modes(Mode.REDUCE_RISK_ONLY, Mode.OBSERVE_ONLY) == Mode.OBSERVE_ONLY


def test_detector_clock_skew_uses_dedicated_threshold() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    detector = AnomalyDetectorService(
        config=AnomalyDetectorConfig(clock_skew_seconds_threshold=5),
        now_provider=lambda: now,
    )
    snapshot = PnLSnapshot(
        total_equity_try=Decimal("1000"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now - timedelta(seconds=10),
        realized_total_try=Decimal("0"),
    )
    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={"TRY": Decimal("0")},
        per_symbol=[],
        equity_estimate=Decimal("1000"),
    )
    events = detector.detect(
        market_data_age_seconds=None,
        reject_count=0,
        cycle_duration_ms=None,
        cursor_stall_by_symbol={},
        pnl_snapshot=snapshot,
        pnl_report=pnl_report,
    )
    assert any(event.code == AnomalyCode.CLOCK_SKEW for event in events)
