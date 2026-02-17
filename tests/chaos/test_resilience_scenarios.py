from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from btcbot.adapters.btcturk.instrumentation import InMemoryMetricsSink
from btcbot.adapters.btcturk.ws_client import BtcturkWsClient, WsIdleTimeoutError
from btcbot.domain.anomalies import AnomalyCode
from btcbot.domain.stage4 import PnLSnapshot
from btcbot.services.anomaly_detector_service import AnomalyDetectorConfig, AnomalyDetectorService
from btcbot.services.ledger_service import PnlReport
from btcbot.services.retry import parse_retry_after_seconds, retry_with_backoff


class _StormSocket:
    def __init__(self, drops_before_message: int) -> None:
        self.drops_before_message = drops_before_message
        self.closed = False

    async def send(self, payload: str) -> None:
        _ = payload

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if self.drops_before_message > 0:
            self.drops_before_message -= 1
            raise WsIdleTimeoutError("forced disconnect")
        return json.dumps({"channel": 423, "event": "trade", "data": {"p": "1"}})

    def close(self) -> None:
        self.closed = True


def test_ws_reconnect_storm_increments_metrics() -> None:
    metrics = InMemoryMetricsSink()

    async def _connect(_: str) -> _StormSocket:
        return _StormSocket(drops_before_message=2)

    async def _noop(_: object) -> None:
        return None

    client = BtcturkWsClient(
        url="wss://example",
        subscription_factory=lambda: [],
        message_handlers={423: _noop},
        metrics=metrics,
        connect_fn=_connect,
        idle_reconnect_seconds=0.001,
        max_backoff_seconds=0.001,
        base_backoff_seconds=0.001,
    )

    async def _run() -> None:
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.02)
        await client.shutdown()
        task.cancel()

    asyncio.run(_run())
    assert metrics.counters.get("ws_reconnects", 0) > 0


def test_rest_429_retry_after_is_honored() -> None:
    attempts = {"n": 0}
    delays: list[float] = []

    def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            response = httpx.Response(429, headers={"Retry-After": "0.2"})
            request = httpx.Request("GET", "https://api.test")
            raise httpx.HTTPStatusError("429", request=request, response=response)
        return "ok"

    def _sleep(seconds: float) -> None:
        delays.append(seconds)

    result = retry_with_backoff(
        _fn,
        max_attempts=4,
        base_delay_ms=10,
        max_delay_ms=1000,
        jitter_seed=1,
        retry_on_exceptions=(httpx.HTTPStatusError,),
        sleep_fn=_sleep,
        retry_after_getter=lambda exc: exc.response.headers.get("Retry-After"),  # type: ignore[attr-defined]
    )
    assert result == "ok"
    assert delays and all(delay >= 0.19 for delay in delays)


def test_retry_after_http_date_parsed() -> None:
    future = (datetime.now(UTC) + timedelta(seconds=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = parse_retry_after_seconds(future)
    assert parsed is not None
    assert parsed >= 0


def test_clock_skew_and_reconcile_lag_anomalies() -> None:
    now = datetime.now(UTC)
    detector = AnomalyDetectorService(
        config=AnomalyDetectorConfig(clock_skew_seconds_threshold=5, latency_spike_ms=50),
        now_provider=lambda: now,
    )
    pnl_snapshot = PnLSnapshot(
        total_equity_try=Decimal("100"),
        realized_today_try=Decimal("0"),
        drawdown_pct=Decimal("0"),
        ts=now - timedelta(seconds=10),
        realized_total_try=Decimal("0"),
    )
    pnl_report = PnlReport(
        realized_pnl_total=Decimal("0"),
        unrealized_pnl_total=Decimal("0"),
        fees_total_by_currency={},
        per_symbol=[],
        equity_estimate=Decimal("100"),
    )
    events = detector.detect(
        market_data_age_seconds={"BTCTRY": 1},
        reject_count=0,
        cycle_duration_ms=60,
        cursor_stall_by_symbol={"BTCTRY": 0},
        pnl_snapshot=pnl_snapshot,
        pnl_report=pnl_report,
    )
    codes = {event.code for event in events}
    assert AnomalyCode.CLOCK_SKEW in codes
    assert AnomalyCode.EXCHANGE_LATENCY_SPIKE in codes
