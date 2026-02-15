from __future__ import annotations

from btcbot.services.client_order_id_service import build_exchange_client_id


def test_exchange_client_id_is_btcturk_safe_length() -> None:
    internal = "internal-" + ("x" * 200)
    value = build_exchange_client_id(internal_client_id=internal, symbol="BTC_TRY", side="buy")
    assert len(value) <= 50


def test_exchange_client_id_deterministic_for_same_internal_key() -> None:
    internal = "stage4-internal-id-123"
    first = build_exchange_client_id(internal_client_id=internal, symbol="BTC_TRY", side="buy")
    second = build_exchange_client_id(internal_client_id=internal, symbol="BTC_TRY", side="buy")
    assert first == second
