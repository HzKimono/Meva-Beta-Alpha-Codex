from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.adapters.replay_exchange import ReplayExchangeClient
from btcbot.services.market_data_replay import MarketDataReplay


def _replay() -> MarketDataReplay:
    return MarketDataReplay(
        candles_by_symbol={},
        orderbook_by_symbol={},
        ticker_by_symbol={},
        start_ts=datetime(2024, 1, 1, tzinfo=UTC),
        end_ts=datetime(2024, 1, 1, tzinfo=UTC),
        step_seconds=60,
        seed=1,
    )


def test_replay_exchange_supports_full_balances() -> None:
    client = ReplayExchangeClient(
        replay=_replay(),
        symbols=["BTCTRY"],
        balances={"TRY": Decimal("123.456"), "BTC": Decimal("0.123456789")},
    )

    balances = {item.asset: item.free for item in client.get_balances()}

    assert balances["TRY"] == 123.46
    assert balances["BTC"] == 0.12345679


def test_replay_exchange_uses_pair_info_snapshot_when_provided() -> None:
    client = ReplayExchangeClient(
        replay=_replay(),
        symbols=["BTCTRY"],
        pair_info_snapshot=[
            {
                "pairSymbol": "BTCTRY",
                "numeratorScale": 8,
                "denominatorScale": 2,
                "minTotalAmount": "50",
                "minQuantity": "0.0001",
                "tickSize": "0.1",
                "stepSize": "0.0001",
            }
        ],
    )

    info = client.get_exchange_info()

    assert len(info) == 1
    assert info[0].tick_size == Decimal("0.1")
