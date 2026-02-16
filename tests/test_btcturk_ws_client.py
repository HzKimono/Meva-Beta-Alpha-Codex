from __future__ import annotations

import asyncio
from pathlib import Path

from btcbot.adapters.btcturk.instrumentation import InMemoryMetricsSink
from btcbot.adapters.btcturk.ws_client import BtcturkWsClient


class _FakeSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if not self.messages:
            raise RuntimeError("done")
        return self.messages.pop(0)


def test_ws_client_parses_and_dispatches_fixture_message() -> None:
    fixture = Path("tests/fixtures/btcturk_ws/channel_423_trade_match.json").read_text().strip()
    received: list[int] = []
    metrics = InMemoryMetricsSink()
    socket = _FakeSocket(messages=[fixture])

    async def _handler(envelope) -> None:  # type: ignore[no-untyped-def]
        received.append(envelope.channel)

    async def _connect(_: str) -> object:
        return socket

    client = BtcturkWsClient(
        url="wss://example.test",
        subscription_factory=lambda: [{"type": 151, "channel": 423, "join": True}],
        message_handlers={423: _handler},
        metrics=metrics,
        connect_fn=_connect,
        ping_interval_seconds=60,
    )

    async def _run_once() -> None:
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)
        await client.shutdown()
        await asyncio.sleep(0)
        task.cancel()

    asyncio.run(_run_once())
    assert received == [423]
