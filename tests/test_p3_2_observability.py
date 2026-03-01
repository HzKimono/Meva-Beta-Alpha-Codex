from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.btcturk.instrumentation import InMemoryMetricsSink
from btcbot.adapters.btcturk.ws_client import BtcturkWsClient
from btcbot.config import Settings
from btcbot.domain.models import PairInfo
from btcbot.services.dynamic_universe_service import DynamicUniverseService
from btcbot.services.stage4_cycle_runner import Stage4CycleRunner
from btcbot.services.state_store import StateStore


class _FakeInstrumentation:
    def __init__(self) -> None:
        self.counters: list[tuple[str, int, dict[str, object] | None]] = []
        self.gauges: list[tuple[str, float, dict[str, object] | None]] = []

    def counter(self, name: str, value: int = 1, *, attrs=None) -> None:  # type: ignore[no-untyped-def]
        self.counters.append((name, value, attrs))

    def gauge(self, name: str, value: float, *, attrs=None) -> None:  # type: ignore[no-untyped-def]
        self.gauges.append((name, value, attrs))

    def histogram(self, name: str, value: float, *, attrs=None) -> None:  # type: ignore[no-untyped-def]
        del name, value, attrs


class _NoopSocket:
    async def send(self, payload: str) -> None:
        del payload

    async def recv(self) -> str:
        raise RuntimeError("unused")

    def close(self) -> None:
        return None


class _UniverseClient:
    def get_exchange_info(self) -> list[PairInfo]:
        return [
            PairInfo(
                pairSymbol="AAAATRY",
                numeratorScale=6,
                denominatorScale=2,
                minTotalAmount=Decimal("10"),
                status="TRADING",
            )
        ]

    def get_orderbook(self, symbol: str) -> tuple[str, str]:
        del symbol
        raise RuntimeError("orderbook unavailable")


class _UniverseExchange:
    def __init__(self) -> None:
        self.client = _UniverseClient()


def test_ws_reconnect_storm_detection_dedupes_logs(monkeypatch, caplog) -> None:
    fake_instr = _FakeInstrumentation()
    monkeypatch.setattr("btcbot.adapters.btcturk.ws_client.get_instrumentation", lambda: fake_instr)

    now_value = 1000.0

    def _now() -> float:
        return now_value

    client = BtcturkWsClient(
        url="wss://example.test",
        subscription_factory=lambda: [],
        message_handlers={},
        metrics=InMemoryMetricsSink(),
        connect_fn=lambda _: _NoopSocket(),  # type: ignore[arg-type]
        ws_reconnect_storm_threshold=3,
        ws_reconnect_storm_window_seconds=60,
        ws_reconnect_storm_log_cooldown_seconds=120,
        now_fn=_now,
    )

    caplog.set_level(logging.WARNING)
    for _ in range(3):
        client._record_reconnect_event(last_exception=RuntimeError("boom"))

    storm_counts = [item for item in fake_instr.counters if item[0] == "ws_reconnect_storm_total"]
    assert storm_counts
    assert len([r for r in caplog.records if r.message == "ws_reconnect_storm"]) == 1

    # Inside cooldown: counter can increase, log should stay deduped.
    client._record_reconnect_event(last_exception=RuntimeError("boom"))
    assert len([r for r in caplog.records if r.message == "ws_reconnect_storm"]) == 1


def test_dynamic_universe_empty_selection_emits_reason_metrics(monkeypatch, tmp_path) -> None:
    fake_instr = _FakeInstrumentation()
    monkeypatch.setattr(
        "btcbot.services.dynamic_universe_service.get_instrumentation", lambda: fake_instr
    )

    now = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    store = StateStore(db_path=str(tmp_path / "state.db"))
    settings = Settings(
        DRY_RUN=True,
        KILL_SWITCH=False,
        SYMBOLS="[]",
        UNIVERSE_TOP_N=2,
    )

    result = DynamicUniverseService().select(
        exchange=_UniverseExchange(),
        state_store=store,
        settings=settings,
        now_utc=now,
        cycle_id="c-empty",
    )

    assert result.selected_symbols == ()
    assert any(name == "universe_empty_selection_total" for name, _, _ in fake_instr.counters)
    assert any(
        name == "universe_empty_reason_total" and (attrs or {}).get("reason") == "orderbook_unavailable"
        for name, _, attrs in fake_instr.counters
    )


def test_mark_price_coverage_ratio_and_threshold_counter() -> None:
    ratio = Stage4CycleRunner._compute_mark_price_coverage_ratio(
        covered_symbols=["A", "B"],
        tradeable_symbols_requested=["A", "B", "C", "D"],
    )
    assert ratio == 0.5
    assert ratio < 0.8
