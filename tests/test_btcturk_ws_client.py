from __future__ import annotations

import asyncio
import json

from btcbot.adapters.btcturk.instrumentation import InMemoryMetricsSink
from btcbot.adapters.btcturk.ws_client import BtcturkWsClient, WsIdleTimeoutError


class _FakeSocket:
    def __init__(self, messages: list[str] | None = None) -> None:
        self.messages = messages or []
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if not self.messages:
            raise WsIdleTimeoutError("done")
        return self.messages.pop(0)

    def close(self) -> None:
        self.closed = True


def _build_client(
    socket: _FakeSocket,
    metrics: InMemoryMetricsSink,
    *,
    queue_max: int = 100,
) -> BtcturkWsClient:
    async def _connect(_: str) -> _FakeSocket:
        return socket

    async def _noop(_: object) -> None:
        return None

    return BtcturkWsClient(
        url="wss://example.test",
        subscription_factory=lambda: [{"type": 151, "channel": 423, "join": True}],
        message_handlers={423: _noop},
        metrics=metrics,
        connect_fn=_connect,
        queue_maxsize=queue_max,
        idle_reconnect_seconds=0.01,
    )


def test_parses_dict_envelope() -> None:
    metrics = InMemoryMetricsSink()
    client = _build_client(_FakeSocket(), metrics)
    msg = json.dumps({"channel": 423, "event": "trade", "data": {"foo": "bar"}})
    envelope = client._parse_message(msg)
    assert envelope is not None
    assert envelope.channel == 423
    assert envelope.event == "trade"
    assert envelope.data == {"foo": "bar"}


def test_parses_compact_array_envelope() -> None:
    metrics = InMemoryMetricsSink()
    client = _build_client(_FakeSocket(), metrics)
    msg = json.dumps([423, {"event": "trade", "data": [1, 2, 3]}])
    envelope = client._parse_message(msg)
    assert envelope is not None
    assert envelope.channel == 423
    assert envelope.event == "trade"
    assert envelope.data == [1, 2, 3]


def test_handler_exception_isolated() -> None:
    metrics = InMemoryMetricsSink()
    socket = _FakeSocket()

    async def _connect(_: str) -> _FakeSocket:
        return socket

    handled: list[str] = []

    async def _bad_handler(_: object) -> None:
        raise RuntimeError("boom")

    async def _good_handler(envelope) -> None:  # type: ignore[no-untyped-def]
        handled.append(str(envelope.event))

    client = BtcturkWsClient(
        url="wss://example.test",
        subscription_factory=lambda: [],
        message_handlers={423: _bad_handler, 424: _good_handler},
        metrics=metrics,
        connect_fn=_connect,
    )

    async def _run() -> None:
        await client.queue.put(client._parse_message('{"channel":423,"event":"x","data":1}') )  # type: ignore[arg-type]
        await client.queue.put(client._parse_message('{"channel":424,"event":"y","data":2}') )  # type: ignore[arg-type]
        task = asyncio.create_task(client._dispatch_loop())
        await asyncio.sleep(0.05)
        await client.shutdown()
        task.cancel()

    asyncio.run(_run())
    assert metrics.counters["ws_handler_errors"] == 1
    assert handled == ["y"]


def test_bounded_queue_drop_increments_metric() -> None:
    metrics = InMemoryMetricsSink()
    socket = _FakeSocket(messages=[
        '{"channel":423,"event":"trade","data":1}',
        '{"channel":423,"event":"trade","data":2}',
    ])
    client = _build_client(socket, metrics, queue_max=1)

    async def _run() -> None:
        try:
            await client._read_loop(socket)
        except WsIdleTimeoutError:
            return

    asyncio.run(_run())
    assert metrics.counters["ws_backpressure_drops"] >= 1


def test_graceful_close_called_on_shutdown() -> None:
    metrics = InMemoryMetricsSink()
    socket = _FakeSocket(messages=[])

    async def _connect(_: str) -> _FakeSocket:
        return socket

    client = BtcturkWsClient(
        url="wss://example.test",
        subscription_factory=lambda: [],
        message_handlers={},
        metrics=metrics,
        connect_fn=_connect,
        idle_reconnect_seconds=0.01,
    )

    async def _run() -> None:
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)
        await client.shutdown()
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(_run())
    assert socket.closed is True
