from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from btcbot.adapters.btcturk.instrumentation import MetricsSink
from btcbot.obs.metrics import inc_counter
from btcbot.obs.process_role import coerce_process_role, get_process_role_from_env
from btcbot.observability import get_instrumentation

logger = logging.getLogger(__name__)


class WsSocket(Protocol):
    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str: ...

    def close(self) -> object: ...


@dataclass(frozen=True)
class WsEnvelope:
    channel: int
    event: str
    data: object
    raw: object


class WsIdleTimeoutError(RuntimeError):
    """Raised when websocket connection is idle for longer than configured threshold."""


class BtcturkWsClient:
    def __init__(
        self,
        *,
        url: str,
        subscription_factory: Callable[[], list[dict[str, object]]],
        message_handlers: dict[int, Callable[[WsEnvelope], Awaitable[None]]],
        metrics: MetricsSink,
        connect_fn: Callable[[str], Awaitable[WsSocket]],
        queue_maxsize: int = 1_000,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        idle_reconnect_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        heartbeat_payload_factory: Callable[[], str] | None = None,
        process_role: str | None = None,
    ) -> None:
        self.url = url
        self.subscription_factory = subscription_factory
        self.message_handlers = message_handlers
        self.metrics = metrics
        self.connect_fn = connect_fn
        self.queue: asyncio.Queue[WsEnvelope] = asyncio.Queue(maxsize=queue_maxsize)
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.idle_reconnect_seconds = idle_reconnect_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.process_role = coerce_process_role(
            process_role or get_process_role_from_env().value
        ).value
        self.heartbeat_payload_factory = heartbeat_payload_factory

        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._last_message_ts = monotonic()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            socket: WsSocket | None = None
            self._tasks = []
            try:
                socket = await self.connect_fn(self.url)
                attempt = 0
                self._last_message_ts = monotonic()
                await self._send_subscriptions(socket)
                self._tasks = [
                    asyncio.create_task(self._read_loop(socket)),
                    asyncio.create_task(self._dispatch_loop()),
                ]
                if self.heartbeat_interval_seconds and self.heartbeat_payload_factory is not None:
                    self._tasks.append(asyncio.create_task(self._heartbeat_loop(socket)))
                done, pending = await asyncio.wait(
                    self._tasks,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    err = task.exception()
                    if err is not None:
                        raise err
            except Exception:
                self.metrics.inc("ws_drops")
                inc_counter(
                    "bot_ws_disconnects_total",
                    labels={"exchange": "btcturk", "process_role": self.process_role},
                )
                logger.exception("BTCTurk websocket disconnected")
            finally:
                await self._cancel_tasks()
                if socket is not None:
                    await self._close_socket(socket)

            attempt += 1
            self.metrics.inc("ws_reconnects")
            self.metrics.inc("ws_reconnect_rate")

            if self._stop.is_set():
                break

            with get_instrumentation().trace("ws_reconnect", attrs={"attempt": attempt}):
                await asyncio.sleep(self._compute_backoff(attempt))

    async def shutdown(self) -> None:
        self._stop.set()
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        for task in self._tasks:
            if task.done():
                continue
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

    async def _close_socket(self, socket: WsSocket) -> None:
        close_result = socket.close()
        if inspect.isawaitable(close_result):
            await close_result

    async def _send_subscriptions(self, socket: WsSocket) -> None:
        for msg in self.subscription_factory():
            await socket.send(json.dumps(msg))

    async def subscribe(self, socket: WsSocket, *, channel: int, event: str, join: bool) -> None:
        payload = {"type": 151, "channel": channel, "event": event, "join": join}
        await socket.send(json.dumps(payload))

    async def _read_loop(self, socket: WsSocket) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=self.idle_reconnect_seconds)
            except TimeoutError as exc:
                raise WsIdleTimeoutError("ws idle timeout") from exc
            self._last_message_ts = monotonic()
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
            try:
                await handler(envelope)
            except Exception:
                self.metrics.inc("ws_handler_errors")
                logger.exception(
                    "ws handler failed",
                    extra={"extra": {"channel": envelope.channel, "event": envelope.event}},
                )

    async def _heartbeat_loop(self, socket: WsSocket) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.heartbeat_interval_seconds or 0)
            payload = self.heartbeat_payload_factory() if self.heartbeat_payload_factory else ""
            await socket.send(payload)

    def _compute_backoff(self, attempt: int) -> float:
        base = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** (attempt - 1)))
        return base * (0.8 + random.random() * 0.4)

    def _parse_message(self, raw: str) -> WsEnvelope | None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.metrics.inc("ws_invalid_messages")
            return None

        if isinstance(payload, dict):
            return self._parse_dict_envelope(payload)
        if isinstance(payload, list):
            return self._parse_array_envelope(payload)

        self.metrics.inc("ws_invalid_messages")
        return None

    def _parse_dict_envelope(self, payload: dict[str, object]) -> WsEnvelope | None:
        channel = payload.get("channel")
        event = payload.get("event")
        if not isinstance(channel, int) or not isinstance(event, str):
            self.metrics.inc("ws_invalid_messages")
            return None
        return WsEnvelope(
            channel=channel,
            event=event,
            data=payload.get("data"),
            raw=payload,
        )

    def _parse_array_envelope(self, payload: list[object]) -> WsEnvelope | None:
        if len(payload) < 2:
            self.metrics.inc("ws_invalid_messages")
            return None

        head, body = payload[0], payload[1]
        if isinstance(body, dict):
            channel = body.get("channel", head if isinstance(head, int) else None)
            event = body.get("event")
            if event is None and isinstance(head, str):
                event = head
            if not isinstance(channel, int) or not isinstance(event, str):
                self.metrics.inc("ws_invalid_messages")
                return None
            return WsEnvelope(
                channel=channel,
                event=event,
                data=body.get("data", body.get("payload")),
                raw=payload,
            )

        if len(payload) >= 3 and isinstance(payload[0], int) and isinstance(payload[1], str):
            return WsEnvelope(
                channel=payload[0],
                event=payload[1],
                data=payload[2],
                raw=payload,
            )

        self.metrics.inc("ws_invalid_messages")
        return None
