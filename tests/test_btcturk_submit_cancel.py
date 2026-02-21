from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.domain.models import ExchangeError, OrderSide, ValidationError


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


def test_submit_limit_order_invalid_quantity_scale_caught_before_request() -> None:
    calls = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v2/server/exchangeinfo":
            calls["get"] += 1
            return httpx.Response(200, json={"success": True, "data": []})
        if request.method == "POST" and request.url.path == "/api/v1/order":
            calls["post"] += 1
            return httpx.Response(500)
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
        live_rules_require_exchangeinfo=False,
    )

    with pytest.raises(ValidationError, match="quantity scale violation"):
        client.submit_limit_order(
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("0.000000001"),
            client_order_id="coid-scale-qty",
        )

    assert calls["get"] >= 1
    assert calls["post"] == 0
    client.close()


def test_submit_limit_order_invalid_price_scale_caught_before_request() -> None:
    calls = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v2/server/exchangeinfo":
            calls["get"] += 1
            return httpx.Response(200, json={"success": True, "data": []})
        if request.method == "POST" and request.url.path == "/api/v1/order":
            calls["post"] += 1
            return httpx.Response(500)
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
        live_rules_require_exchangeinfo=False,
    )

    with pytest.raises(ValidationError, match="price scale violation"):
        client.submit_limit_order(
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100.123456789"),
            qty=Decimal("0.01"),
            client_order_id="coid-scale-price",
        )

    assert calls["get"] >= 1
    assert calls["post"] == 0
    client.close()


def test_submit_limit_order_400_logs_error_body(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v2/server/exchangeinfo":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "pairSymbol": "BTCTRY",
                            "numeratorScale": 8,
                            "denominatorScale": 2,
                            "minTotalAmount": "10",
                        }
                    ],
                },
            )
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

    caplog.set_level("ERROR", logger="btcbot.adapters.btcturk_http")
    with pytest.raises(ExchangeError):
        client.submit_limit_order(
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100.12"),
            qty=Decimal("0.2"),
            client_order_id="coid-400-log",
        )

    assert any("BTCTurk submit_limit_order failed" in r.getMessage() for r in caplog.records)
    logged = [r for r in caplog.records if r.getMessage() == "BTCTurk submit_limit_order failed"]
    payload = getattr(logged[-1], "extra", {})
    assert payload["status_code"] == 400
    assert payload["error_code"] == 1126
    assert payload["pairSymbol"] == "BTCTRY"
    assert payload["quantized_price"] == "100.12"
    assert payload["clientOrderId"] == "coid-400-log"
    assert "FAILED_INVALID_PRICE_SCALE" in payload["response_body"]
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


def test_submit_limit_order_non_positive_values_caught_before_request() -> None:
    calls = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/order":
            calls["post"] += 1
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    with pytest.raises(ValidationError, match="price must be positive"):
        client.submit_limit_order(
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("0"),
            qty=Decimal("0.1"),
            client_order_id="coid-non-positive-price",
        )

    with pytest.raises(ValidationError, match="quantity must be positive"):
        client.submit_limit_order(
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("0"),
            client_order_id="coid-non-positive-qty",
        )

    assert calls["post"] == 0
    client.close()


def test_submit_limit_order_live_requires_exchangeinfo_and_blocks_post() -> None:
    calls = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v2/server/exchangeinfo":
            calls["get"] += 1
            return httpx.Response(200, json={"success": True, "data": []})
        if request.method == "POST" and request.url.path == "/api/v1/order":
            calls["post"] += 1
            return httpx.Response(200, json={"success": True, "data": {"id": 1}})
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c2VjcmV0",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
        live_rules_require_exchangeinfo=True,
    )

    with pytest.raises(ValidationError, match="exchangeinfo_missing_symbol_rules"):
        client.submit_limit_order(
            symbol="BTC_TRY",
            side="buy",
            price=Decimal("100"),
            qty=Decimal("0.01"),
            client_order_id="coid-live-require-rules",
        )

    assert calls["get"] >= 1
    assert calls["post"] == 0
    client.close()
