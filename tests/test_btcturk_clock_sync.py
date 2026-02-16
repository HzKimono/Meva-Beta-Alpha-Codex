from __future__ import annotations

import asyncio

from btcbot.adapters.btcturk.clock_sync import ClockSyncService


class _Provider:
    def __init__(self, server_time_ms: int) -> None:
        self.server_time_ms = server_time_ms

    async def fetch_server_time_ms(self) -> int:
        return self.server_time_ms


def test_clock_sync_applies_offset_to_stamp() -> None:
    service = ClockSyncService(
        provider=_Provider(server_time_ms=102_500),
        max_abs_offset_ms=10_000_000,
    )
    service.utc_now_ms = lambda: 100_000  # type: ignore[method-assign]
    asyncio.run(service.sync())
    assert service.stamped_now_ms() == 102_500
