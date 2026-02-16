from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class ServerTimeProvider(Protocol):
    async def fetch_server_time_ms(self) -> int: ...


@dataclass
class ClockSyncService:
    provider: ServerTimeProvider
    refresh_interval_seconds: int = 60
    max_abs_offset_ms: int = 15_000

    def __post_init__(self) -> None:
        self._offset_ms = 0
        self._last_sync_ms = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def utc_now_ms() -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    async def sync(self) -> int:
        async with self._lock:
            local_ms = self.utc_now_ms()
            server_ms = int(await self.provider.fetch_server_time_ms())
            self._offset_ms = server_ms - local_ms
            self._last_sync_ms = local_ms
            if abs(self._offset_ms) > self.max_abs_offset_ms:
                # TODO: route through anomaly service once adapter wiring is complete.
                self._offset_ms = max(
                    -self.max_abs_offset_ms,
                    min(self._offset_ms, self.max_abs_offset_ms),
                )
            return self._offset_ms

    async def maybe_sync(self) -> int:
        if self.utc_now_ms() - self._last_sync_ms >= self.refresh_interval_seconds * 1000:
            return await self.sync()
        return self._offset_ms

    def stamped_now_ms(self) -> int:
        return self.utc_now_ms() + self._offset_ms
