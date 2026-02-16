from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from btcbot.adapters.btcturk.instrumentation import MetricsSink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WsEnvelope:
    channel: int
    event: str
    data: dict[str, object]


class BtcturkWsClient:
    def __init__(
        self,
        *,
        url: str,
        subscription_factory: Callable[[], list[dict[str, object]]],
        message_handlers: dict[int, Callable[[WsEnvelope], Awaitable[None]]],
        metrics: MetricsSink,
        connect_fn: Callable[[str], Awaitable[object]],
        queue_maxsize: int = 1_000,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        ping_interval_seconds: float = 15.0,
    ) -> None:
        self.url = url
        self.subscription_factory = subscription_factory
        self.message_handlers = message_handlers
        self.metrics = metrics
        self.connect_fn = connect_fn
        self.queue: asyncio.Queue[WsEnvelope] = asyncio.Queue(maxsize=queue_maxsize)
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.ping_interval_seconds = ping_interval_seconds
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                socket = await self.connect_fn(self.url)
                attempt = 0
                await self._send_subscriptions(socket)
                self._tasks = [
                    asyncio.create_task(self._read_loop(socket)),
                    asyncio.create_task(self._dispatch_loop()),
                    asyncio.create_task(self._heartbeat_loop(socket)),
                ]
                await asyncio.wait(self._tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in self._tasks:
                    task.cancel()
                self.metrics.inc("ws_drops")
            except Exception:
                self.metrics.inc("ws_drops")
                logger.exception("BTCTurk websocket disconnected")
            if self._stop.is_set():
                break
            attempt += 1
            delay = self._compute_backoff(attempt)
            self.metrics.inc("ws_reconnects")
            await asyncio.sleep(delay)

    async def shutdown(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()

    async def _send_subscriptions(self, socket: object) -> None:
        send = socket.send
        for msg in self.subscription_factory():
            await send(json.dumps(msg))

    async def subscribe(self, socket: object, *, channel: int, event: str, join: bool) -> None:
        send = socket.send
        payload = {"type": 151, "channel": channel, "event": event, "join": join}
        await send(json.dumps(payload))

    def _compute_backoff(self, attempt: int) -> float:
        base = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** (attempt - 1)))
        return base * (0.8 + random.random() * 0.4)

    async def _read_loop(self, socket: object) -> None:
        recv = socket.recv
        while not self._stop.is_set():
            raw = await recv()
            envelope = self._parse_message(raw)
            if envelope is None:
                continue
            try:
                self.queue.put_nowait(envelope)
            except asyncio.QueueFull:
                self.metrics.inc("ws_backpressure_drops")

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            envelope = await self.queue.get()
            handler = self.message_handlers.get(envelope.channel)
            if handler is None:
                self.metrics.inc("ws_unhandled_messages")
                continue
            await handler(envelope)

    async def _heartbeat_loop(self, socket: object) -> None:
        send = socket.send
        while not self._stop.is_set():
            start = monotonic()
            await send(json.dumps({"type": "ping"}))
            self.metrics.observe_ms("ws_ping_interval", (monotonic() - start) * 1000)
            await asyncio.sleep(self.ping_interval_seconds)

    def _parse_message(self, raw: str) -> WsEnvelope | None:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        channel = payload.get("channel")
        event = payload.get("event")
        data = payload.get("data")
        if not isinstance(channel, int) or not isinstance(event, str) or not isinstance(data, dict):
            self.metrics.inc("ws_invalid_messages")
            return None
        return WsEnvelope(channel=channel, event=event, data=data)
