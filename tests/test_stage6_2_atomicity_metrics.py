from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btcbot.config import Settings
from btcbot.domain.accounting import TradeFill
from btcbot.domain.execution_quality import compute_execution_quality
from btcbot.domain.models import OrderSide, PairInfo
from btcbot.domain.stage4 import Fill
from btcbot.services import metrics_service
from btcbot.services import stage4_cycle_runner as runner_module
from btcbot.services.accounting_service_stage4 import AccountingService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner, Stage4ExchangeError
from btcbot.services.state_store import StateStore


class ExchangeForAtomicity:
    def __init__(self) -> None:
        self.fill_ts = datetime.now(UTC)

    def get_orderbook(self, symbol: str) -> tuple[float, float]:
        del symbol
        return (100.0, 101.0)

    def get_balances(self):
        return [type("B", (), {"asset": "TRY", "free": Decimal("1000")})()]

    def list_open_orders(self, symbol: str):
        del symbol
        return []

    def get_recent_fills(self, symbol: str, since_ms: int | None = None):
        del since_ms
        return [
            TradeFill(
                fill_id=f"fill-{symbol}-1",
                order_id=f"order-{symbol}",
                symbol=symbol,
                side=OrderSide.BUY,
                price=Decimal("100"),
                qty=Decimal("0.1"),
                fee=Decimal("1"),
                fee_currency="TRY",
                ts=self.fill_ts,
            )
        ]

    def get_exchange_info(self) -> list[PairInfo]:
        return [
            PairInfo(
                pairSymbol="BTCTRY",
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                tickSize=Decimal("0.1"),
                stepSize=Decimal("0.0001"),
            )
        ]

    def close(self) -> None:
        return


def test_cycle_metrics_persist_called_once(monkeypatch, tmp_path) -> None:
    exchange = ExchangeForAtomicity()
    runner = Stage4CycleRunner()
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(tmp_path / "persist_once.sqlite"),
        SYMBOLS="BTC_TRY",
    )

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    original_persist_cycle_metrics = metrics_service.persist_cycle_metrics
    call_counter = {"count": 0}

    def spy_persist_cycle_metrics(state_store, cycle_metrics):
        call_counter["count"] += 1
        return original_persist_cycle_metrics(state_store, cycle_metrics)

    monkeypatch.setattr(metrics_service, "persist_cycle_metrics", spy_persist_cycle_metrics)

    assert runner.run_one_cycle(settings) == 0
    assert call_counter["count"] == 1


def test_cycle_transaction_atomicity_and_recovery(monkeypatch, tmp_path) -> None:
    exchange = ExchangeForAtomicity()
    runner = Stage4CycleRunner()
    db_path = tmp_path / "atomic.sqlite"
    settings = Settings(
        DRY_RUN=True, KILL_SWITCH=False, STATE_DB_PATH=str(db_path), SYMBOLS="BTC_TRY"
    )

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    class FailingAccountingService(AccountingService):
        def apply_fills(self, fills, *, mark_prices, try_cash):
            raise RuntimeError("forced_failure")

    monkeypatch.setattr(runner_module, "AccountingService", FailingAccountingService)

    with pytest.raises(Stage4ExchangeError):
        runner.run_one_cycle(settings)

    store = StateStore(str(db_path))
    with store._connect() as conn:
        ledger_count = conn.execute("SELECT COUNT(*) AS c FROM ledger_events").fetchone()["c"]
        metrics_count = conn.execute("SELECT COUNT(*) AS c FROM cycle_metrics").fetchone()["c"]
    assert ledger_count == 0
    assert metrics_count == 0
    assert store.get_cursor("fills_cursor:BTCTRY") is None

    monkeypatch.setattr(runner_module, "AccountingService", AccountingService)
    assert runner.run_one_cycle(settings) == 0

    with store._connect() as conn:
        ledger_count_after = conn.execute("SELECT COUNT(*) AS c FROM ledger_events").fetchone()["c"]
        metrics_count_after = conn.execute("SELECT COUNT(*) AS c FROM cycle_metrics").fetchone()[
            "c"
        ]
        metrics_row = conn.execute(
            "SELECT fill_rate, meta_json FROM cycle_metrics ORDER BY ts_start DESC LIMIT 1"
        ).fetchone()
    assert ledger_count_after == 2
    assert metrics_count_after == 1
    assert store.get_cursor("fills_cursor:BTCTRY") is not None
    assert metrics_row is not None
    assert isinstance(metrics_row["fill_rate"], float)

    meta = json.loads(str(metrics_row["meta_json"]))
    assert meta["fill_rate_semantics"] == "fills_per_submitted_order"
    assert isinstance(meta["ledger_events_ignored"], int)
    per_symbol = meta["per_symbol"]
    assert isinstance(per_symbol, list)
    assert isinstance(per_symbol[0]["slippage_bps_avg"], float)


def test_accounting_fill_idempotency_unique_applied_fills(tmp_path) -> None:
    exchange = ExchangeForAtomicity()
    store = StateStore(str(tmp_path / "idempotent.sqlite"))
    service = AccountingService(exchange=exchange, state_store=store)

    fill = service.fetch_new_fills("BTC_TRY").fills
    first = service.apply_fills(
        fill, mark_prices={"BTCTRY": Decimal("100")}, try_cash=Decimal("1000")
    )
    second = service.apply_fills(
        fill, mark_prices={"BTCTRY": Decimal("100")}, try_cash=Decimal("1000")
    )

    assert first.realized_total_try == second.realized_total_try
    pos = store.get_stage4_position("BTC_TRY")
    assert pos is not None
    assert pos.qty == Decimal("0.1")


def test_cursor_not_advanced_when_ingest_fails_mid_transaction(tmp_path) -> None:
    exchange = ExchangeForAtomicity()
    store = StateStore(str(tmp_path / "cursor_atomicity.sqlite"))
    accounting = AccountingService(exchange=exchange, state_store=store)

    fetched = accounting.fetch_new_fills("BTC_TRY")
    assert fetched.cursor_after is not None

    with pytest.raises(RuntimeError, match="forced_mid_ingest_failure"):
        with store.transaction():
            # Simulate failure after fills are fetched but before cursor write.
            accounting.apply_fills(
                fetched.fills,
                mark_prices={"BTCTRY": Decimal("100")},
                try_cash=Decimal("1000"),
            )
            raise RuntimeError("forced_mid_ingest_failure")
            # no cursor write due to failure

    assert store.get_cursor("fills_cursor:BTCTRY") is None


def test_compute_execution_quality_fills_per_submitted_order_and_slippage() -> None:
    ts = datetime.now(UTC)
    fills = [
        Fill(
            fill_id="a",
            order_id="oa",
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("1"),
            fee=Decimal("0"),
            fee_asset="TRY",
            ts=ts,
        ),
        Fill(
            fill_id="b",
            order_id="ob",
            symbol="BTC_TRY",
            side="sell",
            price=Decimal("99"),
            qty=Decimal("1"),
            fee=Decimal("0"),
            fee_asset="TRY",
            ts=ts,
        ),
    ]

    snapshot = compute_execution_quality(
        {"orders_submitted": 4, "orders_canceled": 1, "rejects_count": 1},
        fills,
        {"BTCTRY": Decimal("100")},
    )

    assert snapshot.fills_per_submitted_order == Decimal("0.5")
    assert snapshot.slippage_bps_avg == Decimal("100")


def test_compute_execution_quality_per_symbol_counts_all_fills_with_partial_slippage() -> None:
    ts = datetime.now(UTC)
    fills = [
        Fill(
            fill_id="a",
            order_id="oa",
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("101"),
            qty=Decimal("2"),
            fee=Decimal("0"),
            fee_asset="TRY",
            ts=ts,
        ),
        Fill(
            fill_id="b",
            order_id="ob",
            symbol="BTC_TRY",
            side="hold",
            price=Decimal("98"),
            qty=Decimal("1"),
            fee=Decimal("0"),
            fee_asset="TRY",
            ts=ts,
        ),
    ]

    snapshot = compute_execution_quality(
        {"orders_submitted": 2, "orders_canceled": 0, "rejects_count": 0},
        fills,
        {"BTCTRY": Decimal("100")},
    )

    assert len(snapshot.per_symbol) == 1
    assert snapshot.per_symbol[0].symbol == "BTCTRY"
    assert snapshot.per_symbol[0].fills_count == 2
    assert snapshot.per_symbol[0].slippage_bps_avg == Decimal("100")


def test_cycle_metrics_fills_count_uses_persisted_semantics_for_deduped_fills(
    monkeypatch, tmp_path
) -> None:
    exchange = ExchangeForAtomicity()
    runner = Stage4CycleRunner()
    db_path = tmp_path / "fills_semantics.sqlite"
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        STATE_DB_PATH=str(db_path),
        SYMBOLS="BTC_TRY",
    )

    monkeypatch.setattr(
        "btcbot.services.stage4_cycle_runner.build_exchange_stage4",
        lambda settings, dry_run: exchange,
    )

    assert runner.run_one_cycle(settings) == 0
    assert runner.run_one_cycle(settings) == 0

    store = StateStore(str(db_path))
    with store._connect() as conn:
        row = conn.execute(
            "SELECT fills_count, meta_json FROM cycle_metrics ORDER BY ts_start DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert int(row["fills_count"]) == 0
    meta = json.loads(str(row["meta_json"]))
    assert meta["fills_fetched_count"] >= 1
    assert meta["fills_persisted_count"] == 0
