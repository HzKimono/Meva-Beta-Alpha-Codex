from __future__ import annotations

import json

import httpx
import pytest

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.domain.models import ExchangeError, OrderSide


def test_submit_limit_order_payload_fields() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/order":
            seen["query"] = dict(request.url.params)
            seen["body"] = request.read().decode()
            seen["content_type"] = request.headers.get("content-type", "")
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json={"success": True, "data": {"id": 987}})
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )
    client._next_stamp_ms = lambda: "1700000000123"

    order = client.place_limit_order(
        symbol="BTC_TRY",
        side=OrderSide.BUY,
        price=123.45,
        quantity=0.01,
        client_order_id="meva2-c1-BTCTRY-b-1234abcd",
    )

    assert order.order_id == "987"
    query = seen["query"]
    assert query == {}
    body = json.loads(str(seen["body"]))
    assert body["pairSymbol"] == "BTCTRY"
    assert body["price"] == "123.45"
    assert body["quantity"] == "0.01"
    assert body["orderMethod"] == "limit"
    assert body["orderType"] == "buy"
    assert body["newOrderClientId"] == "meva2-c1-BTCTRY-b-1234abcd"
    assert "application/json" in str(seen["content_type"])

    headers = seen["headers"]
    assert headers["x-pck"] == "demo-key"
    assert headers["x-stamp"] == "1700000000123"
    assert headers["x-signature"]
    client.close()


def test_submit_payload_uses_btcturk_client_id_field() -> None:
    client = BtcturkHttpClient(api_key="demo-key", api_secret="c2VjcmV0")
    payload = client._build_submit_order_payload(
        request=type(
            "Req",
            (),
            {
                "pair_symbol": "BTCTRY",
                "price": 123.45,
                "quantity": 0.01,
                "side": type("S", (), {"value": "buy"})(),
                "client_order_id": "safe-exchange-id",
            },
        )()
    )
    assert payload["newOrderClientId"] == "safe-exchange-id"
    client.close()


def test_cancel_order_payload_contains_id() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path == "/api/v1/order":
            seen["json"] = request.read().decode()
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    assert client.cancel_order("55") is True
    assert seen["json"] == '{"id":55}'
    client.close()


def test_place_limit_order_requires_client_order_id() -> None:
    client = BtcturkHttpClient(api_key="demo-key", api_secret="c2VjcmV0")
    with pytest.raises(ValueError, match="client_order_id"):
        client.place_limit_order(symbol="BTC_TRY", side=OrderSide.BUY, price=1.0, quantity=1.0)
    client.close()


def test_submit_limit_order_400_includes_code_and_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/order":
            return httpx.Response(
                400,
                json={"success": False, "code": 1126, "message": "FAILED_INVALID_PRICE_SCALE"},
            )
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    with pytest.raises(ExchangeError) as exc_info:
        client.place_limit_order(
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=123.45,
            quantity=0.01,
            client_order_id="coid-1",
        )

    message = str(exc_info.value)
    assert "status=400" in message
    assert "code=1126" in message
    assert "FAILED_INVALID_PRICE_SCALE" in message
    client.close()


def test_submit_limit_order_415_exposes_sanitized_request_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/order":
            assert dict(request.url.params) == {}
            assert request.read().decode()
            return httpx.Response(
                415,
                json={"code": "UNSUPPORTED_MEDIA_TYPE", "message": "Unsupported Media Type"},
            )
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    with pytest.raises(ExchangeError) as exc_info:
        client.place_limit_order(
            symbol="BTC_TRY",
            side=OrderSide.BUY,
            price=100.0,
            quantity=0.01,
            client_order_id="coid-415",
        )

    exc = exc_info.value
    assert exc.status_code == 415
    assert exc.request_json is not None
    assert exc.request_json["pairSymbol"] == "BTCTRY"
    assert exc.request_json["price"] == "100"
    assert exc.request_json["quantity"] == "0.01"
    assert exc.request_json["newOrderClientId"] == "coid-415"
    client.close()
