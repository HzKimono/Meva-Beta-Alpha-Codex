from __future__ import annotations

from decimal import Decimal

from btcbot.services.account_snapshot_service import AccountSnapshotService


class _Balance:
    def __init__(self, asset: str, free: str, locked: str = "0") -> None:
        self.asset = asset
        self.free = Decimal(free)
        self.locked = Decimal(locked)


class FakeSnapshotExchange:
    def get_balances(self):
        return [_Balance("TRY", "1000", "50"), _Balance("BTC", "0.01", "0.001")]

    def get_orderbook(self, symbol: str):
        if symbol == "BTCTRY":
            return (Decimal("3000000"), Decimal("3001000"))
        return (Decimal("100"), Decimal("101"))


def test_snapshot_service_equity_and_flags() -> None:
    svc = AccountSnapshotService(exchange=FakeSnapshotExchange())
    snapshot = svc.build_snapshot(symbols=["BTCTRY"], fallback_try_cash=Decimal("777"))

    assert snapshot.cash_try == Decimal("1000")
    assert snapshot.total_equity_try > snapshot.cash_try
    assert "private:get_balances" in snapshot.source_endpoints
    assert "public:get_orderbook" in snapshot.source_endpoints
    assert "missing_private_data" not in snapshot.flags
