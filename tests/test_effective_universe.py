from __future__ import annotations

from btcbot.config import Settings
from btcbot.services.effective_universe import resolve_effective_universe


class _FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    def get_exchange_info(self):
        return [
            {"name": "BTCTRY"},
            {"name": "ETHTRY"},
            {"name": "SOLTRY"},
            {"name": "XRPTRY"},
            {"name": "ADATRY"},
        ]

    def close(self) -> None:
        return None


class _FailingClient(_FakeClient):
    def get_exchange_info(self):
        raise RuntimeError("unavailable")


def test_resolve_effective_universe_rejects_and_suggests(monkeypatch) -> None:
    monkeypatch.setattr("btcbot.services.effective_universe.BtcturkHttpClient", _FakeClient)

    settings = Settings(UNIVERSE_SYMBOLS="BTCTRY,ETHTRY,SOLTRY,XRPTTRY,ADATRY")
    resolved = resolve_effective_universe(settings)

    assert resolved.symbols == ["BTCTRY", "ETHTRY", "SOLTRY", "ADATRY"]
    assert resolved.rejected_symbols == ["XRPTTRY"]
    assert resolved.suggestions["XRPTTRY"][0] == "XRPTRY"
    assert resolved.auto_corrected_symbols == {}


def test_resolve_effective_universe_autocorrects_unambiguous_symbol(monkeypatch) -> None:
    monkeypatch.setattr("btcbot.services.effective_universe.BtcturkHttpClient", _FakeClient)

    settings = Settings(
        UNIVERSE_SYMBOLS="BTCTRY,ETHTRY,SOLTRY,XRPTTRY,ADATRY",
        UNIVERSE_AUTO_CORRECT=True,
    )
    resolved = resolve_effective_universe(settings)

    assert resolved.symbols == ["BTCTRY", "ETHTRY", "SOLTRY", "XRPTRY", "ADATRY"]
    assert resolved.auto_corrected_symbols == {"XRPTTRY": "XRPTRY"}


def test_resolve_effective_universe_when_metadata_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("btcbot.services.effective_universe.BtcturkHttpClient", _FailingClient)

    settings = Settings(UNIVERSE_SYMBOLS="BTCTRY,ETHTRY")
    resolved = resolve_effective_universe(settings)

    assert resolved.symbols == ["BTCTRY", "ETHTRY"]
    assert resolved.metadata_available is False
