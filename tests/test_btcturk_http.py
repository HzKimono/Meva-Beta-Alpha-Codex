from __future__ import annotations

import base64
import hashlib
import hmac
import ssl
from decimal import Decimal

import httpx
import pytest

from btcbot.adapters.btcturk_http import (
    BtcturkHttpClient,
    BtcturkHttpClientStage4,
    ConfigurationError,
    _parse_stage4_open_order_item,
    _should_retry,
)
from btcbot.domain.models import ExchangeError, OrderSide


class DummyResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_should_retry_on_429_and_5xx() -> None:
    req = httpx.Request("GET", "https://example.com")

    too_many = httpx.HTTPStatusError(
        "rate-limited",
        request=req,
        response=httpx.Response(status_code=429, request=req),
    )
    server_error = httpx.HTTPStatusError(
        "server error",
        request=req,
        response=httpx.Response(status_code=500, request=req),
    )
    client_error = httpx.HTTPStatusError(
        "bad request",
        request=req,
        response=httpx.Response(status_code=400, request=req),
    )

    assert _should_retry(too_many) is True
    assert _should_retry(server_error) is True
    assert _should_retry(client_error) is False


def test_get_orderbook_parses_valid_payload(monkeypatch) -> None:
    client = BtcturkHttpClient()

    def fake_get(path: str, params: dict[str, str | int] | None = None) -> dict:
        assert path == "/api/v2/orderbook"
        assert params == {"pairSymbol": "BTCTRY"}
        return {"success": True, "data": {"bids": [["100.25", "1"]], "asks": [["100.5", "2"]]}}

    monkeypatch.setattr(client, "_get", fake_get)

    bid, ask = client.get_orderbook("BTC_TRY")

    assert bid == 100.25
    assert ask == 100.5
    client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "data": "not-a-dict"},
        {"success": True, "data": {"bids": [], "asks": [["1", "1"]]}},
        {"success": True, "data": {"bids": [["1", "1"]], "asks": []}},
        {"success": True, "data": {"bids": [["abc", "1"]], "asks": [["1", "1"]]}},
        {"success": True, "data": {"bids": [["1", "1"]], "asks": [["0", "1"]]}},
    ],
)
def test_get_orderbook_rejects_malformed_payloads(monkeypatch, payload: dict) -> None:
    client = BtcturkHttpClient()
    monkeypatch.setattr(client, "_get", lambda *_args, **_kwargs: payload)

    with pytest.raises(ValueError):
        client.get_orderbook("BTC_TRY")
    client.close()


def test_get_balances_parses_comma_decimal_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/balances":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "asset": "TRY",
                            "balance": "100,50",
                            "locked": "5,25",
                            "free": "95,25",
                            "orderFund": "1,00",
                            "requestFund": "0,50",
                            "precision": 2,
                            "timestamp": 1700000000000,
                            "assetname": "Turkish Lira",
                        }
                    ],
                },
            )
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    balances = client.get_balances()

    assert len(balances) == 1
    assert balances[0].asset == "TRY"
    assert Decimal(str(balances[0].free)) == Decimal("95.25")
    assert Decimal(str(balances[0].locked)) == Decimal("5.25")
    client.close()


def test_get_open_orders_parses_bids_and_asks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/openOrders":
            assert request.url.params["pairSymbol"] == "BTCTRY"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "bids": [
                            {
                                "id": 1,
                                "price": "27223,7283",
                                "amount": "0,01",
                                "quantity": "0,01",
                                "stopPrice": "0",
                                "pairSymbol": "BTCTRY",
                                "pairSymbolNormalized": "BTC_TRY",
                                "type": "limit",
                                "method": "buy",
                                "orderClientId": "cid-1",
                                "time": 1700000000000,
                                "updateTime": 1700000000100,
                                "status": "Untouched",
                                "leftAmount": "0,01",
                            }
                        ],
                        "asks": [
                            {
                                "id": 2,
                                "price": "30000.1",
                                "amount": "0.02",
                                "quantity": "0.02",
                                "stopPrice": "0",
                                "pairSymbol": "BTCTRY",
                                "pairSymbolNormalized": "BTC_TRY",
                                "type": "limit",
                                "method": "sell",
                                "orderClientId": "cid-2",
                                "time": 1700000000200,
                                "updateTime": 1700000000300,
                                "status": "Untouched",
                                "leftAmount": "0.02",
                            }
                        ],
                    },
                },
            )
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    open_orders = client.get_open_orders("BTCTRY")

    assert len(open_orders.bids) == 1
    assert len(open_orders.asks) == 1
    assert str(open_orders.bids[0].price) == "27223.7283"
    assert str(open_orders.asks[0].quantity) == "0.02"
    client.close()


def test_private_methods_raise_configuration_error_when_credentials_missing() -> None:
    client = BtcturkHttpClient()

    with pytest.raises(ConfigurationError, match="BTCTURK_API_KEY"):
        client.get_balances()
    with pytest.raises(ConfigurationError, match="BTCTURK_API_KEY"):
        client.get_open_orders("BTCTRY")

    client.close()


def test_write_private_endpoints_require_credentials() -> None:
    client = BtcturkHttpClient()

    with pytest.raises(ConfigurationError):
        client.cancel_order("1")
    with pytest.raises(ValueError, match="client_order_id"):
        client.place_limit_order("BTC_TRY", side=OrderSide.BUY, price=1.0, quantity=1.0)

    client.close()


def test_private_get_uses_deterministic_signature_headers() -> None:
    api_key = "demo-key"
    api_secret_base64 = "c3VwZXItc2VjcmV0LWJ5dGVz"
    nonce = "1700000000123"

    expected_signature = base64.b64encode(
        hmac.new(
            base64.b64decode(api_secret_base64),
            f"{api_key}{nonce}".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()

    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/balances":
            seen_headers.update(request.headers)
            return httpx.Response(200, json={"success": True, "data": []})
        return httpx.Response(404)

    client = BtcturkHttpClient(
        api_key=api_key,
        api_secret=api_secret_base64,
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    original_next_stamp_ms = client._next_stamp_ms
    client._next_stamp_ms = lambda: nonce
    try:
        balances = client.get_balances()
    finally:
        client._next_stamp_ms = original_next_stamp_ms

    assert balances == []
    assert seen_headers["x-pck"] == api_key
    assert seen_headers["x-stamp"] == nonce
    assert seen_headers["x-signature"] == expected_signature
    client.close()


def test_next_stamp_ms_is_monotonic(monkeypatch) -> None:
    client = BtcturkHttpClient(api_key="k", api_secret="c2VjcmV0")

    values = iter([1700000000000.0, 1700000000000.0, 1700000000000.001])
    monkeypatch.setattr("btcbot.adapters.btcturk_http.time", lambda: next(values))

    first = int(client._next_stamp_ms())
    second = int(client._next_stamp_ms())
    third = int(client._next_stamp_ms())

    assert second == first + 1
    assert third >= second + 1
    client.close()


def test_order_snapshot_side_prefers_order_method_over_method() -> None:
    client = BtcturkHttpClient()
    snapshot = client._to_order_snapshot(
        {
            "id": 1,
            "pairSymbol": "BTCTRY",
            "orderMethod": "buy",
            "method": "limit",
            "price": "100",
            "quantity": "0.1",
            "status": "Untouched",
            "time": 1700000000000,
        }
    )
    assert snapshot.side == OrderSide.BUY


def test_order_snapshot_side_unknown_when_fields_absent() -> None:
    client = BtcturkHttpClient()
    snapshot = client._to_order_snapshot(
        {
            "id": 1,
            "pairSymbol": "BTCTRY",
            "method": "limit",
            "price": "100",
            "quantity": "0.1",
            "status": "Untouched",
            "time": 1700000000000,
        }
    )
    assert snapshot.side is None


def test_should_retry_on_timeout_and_transport_errors() -> None:
    req = httpx.Request("GET", "https://example.com")

    timeout = httpx.ReadTimeout("read timeout", request=req)
    transport = httpx.ConnectError("connect failed", request=req)

    assert _should_retry(timeout) is True
    assert _should_retry(transport) is True


def test_should_not_retry_on_non_transient_errors() -> None:
    assert _should_retry(ValueError("bad payload")) is False


def test_public_get_retries_429_using_retry_after_header(monkeypatch) -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.path == "/api/v2/orderbook" and calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1.5"}, request=request)
        if request.url.path == "/api/v2/orderbook":
            return httpx.Response(
                200,
                json={"success": True, "data": {"bids": [["100", "1"]], "asks": [["101", "1"]]}},
                request=request,
            )
        return httpx.Response(404, request=request)

    monkeypatch.setattr(
        "btcbot.adapters.btcturk_http.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    client = BtcturkHttpClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    bid, ask = client.get_orderbook("BTC_TRY")

    assert (bid, ask) == (100.0, 101.0)
    assert calls["count"] == 2
    assert sleeps == [1.5]
    client.close()


def test_private_write_request_is_not_retried() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.path == "/api/v1/order" and request.method == "DELETE":
            return httpx.Response(500, json={"success": False, "message": "fail"}, request=request)
        return httpx.Response(404, request=request)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )

    with pytest.raises(ExchangeError):
        client.cancel_order("1")

    assert calls["count"] == 1
    client.close()


def test_private_get_retries_429_using_retry_after_header_and_penalizes_limiter(
    monkeypatch,
) -> None:
    class SpyLimiter:
        def __init__(self) -> None:
            self.penalties: list[float | None] = []

        def acquire(self, group: str, cost: int = 1) -> float:
            del group, cost
            return 0.0

        def penalize_on_429(self, group: str, retry_after_s: float | None) -> None:
            del group
            self.penalties.append(retry_after_s)

    calls = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.path == "/api/v1/users/balances" and calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1.5"}, json={}, request=request)
        if request.url.path == "/api/v1/users/balances":
            return httpx.Response(200, json={"success": True, "data": []}, request=request)
        return httpx.Response(404, request=request)

    monkeypatch.setattr("btcbot.adapters.btcturk_http.sleep", lambda seconds: sleeps.append(seconds))

    limiter = SpyLimiter()
    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
        rate_limiter=limiter,  # type: ignore[arg-type]
    )

    balances = client.get_balances()

    assert balances == []
    assert calls["count"] == 2
    assert sleeps == [1.5]
    assert limiter.penalties == [1.5]
    client.close()


def test_should_not_retry_on_permanent_transport_error() -> None:
    req = httpx.Request("GET", "https://example.com")
    cert_error = httpx.ConnectError("cert failed", request=req)
    cert_error.__cause__ = ssl.SSLCertVerificationError("hostname mismatch")

    assert _should_retry(cert_error) is False


def test_stage4_open_order_parser_is_decimal_native() -> None:
    client = BtcturkHttpClient()
    parsed = _parse_stage4_open_order_item(
        {
            "id": 11,
            "price": "27223.7283",
            "quantity": "0.0100",
            "pairSymbolNormalized": "BTC_TRY",
            "method": "buy",
            "type": "limit",
            "status": "Untouched",
            "time": 1700000000000,
            "updateTime": 1700000000100,
        },
        side_parser=client._parse_side,
        status_parser=client._parse_exchange_status,
    )
    assert parsed is not None
    assert isinstance(parsed.price, Decimal)
    assert isinstance(parsed.qty, Decimal)
    assert parsed.price == Decimal("27223.7283")
    assert parsed.qty == Decimal("0.0100")


def test_stage4_list_open_orders_does_not_bridge_to_stage3_list() -> None:
    class SpyClient(BtcturkHttpClient):
        def list_open_orders(self, symbol: str | None = None):  # type: ignore[override]
            raise AssertionError("stage3 list_open_orders must not be called")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/openOrders":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "bids": [
                            {
                                "id": 1,
                                "price": "100.1",
                                "quantity": "0.2",
                                "pairSymbolNormalized": "BTC_TRY",
                                "method": "buy",
                                "type": "limit",
                                "status": "Untouched",
                                "time": 1700000000000,
                                "updateTime": 1700000000100,
                            }
                        ],
                        "asks": [],
                    },
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    client = SpyClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )
    adapter = BtcturkHttpClientStage4(client)

    orders = adapter.list_open_orders("BTC_TRY")

    assert len(orders) == 1
    assert orders[0].price == Decimal("100.1")
    assert orders[0].qty == Decimal("0.2")
    client.close()


def test_extract_fill_rows_accepts_empty_payload_variants() -> None:
    client = BtcturkHttpClient()
    payloads = [
        {"success": True},
        {"success": True, "data": []},
        {"success": True, "data": None},
        {"success": True, "data": {}},
        {"success": True, "data": {"items": []}},
    ]

    for payload in payloads:
        assert client._extract_fill_rows(payload, path="/api/v1/users/transactions/trade") == []


def test_extract_fill_rows_treats_missing_list_keys_as_empty_for_fills() -> None:
    client = BtcturkHttpClient()

    assert (
        client._extract_fill_rows(
            {"success": True, "data": {"page": 1, "total": 0}},
            path="/api/v1/users/transactions/trade",
        )
        == []
    )


def test_extract_fill_rows_parses_normal_payload() -> None:
    client = BtcturkHttpClient()

    rows = client._extract_fill_rows(
        {"success": True, "data": [{"id": "1", "orderId": "2"}]},
        path="/api/v1/users/transactions/trade",
    )

    assert rows == [{"id": "1", "orderId": "2"}]


def test_stage4_recent_fills_uses_deterministic_fallback_when_unique_fill_id_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/transactions/trade":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "orderId": "12",
                            "orderClientId": "cid-should-not-be-fill-id",
                            "orderType": "buy",
                            "price": "100",
                            "amount": "0.1",
                            "fee": "0",
                            "feeCurrency": "TRY",
                            "timestamp": 1700000000000,
                        }
                    ],
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )
    adapter = BtcturkHttpClientStage4(client)

    fills = adapter.get_recent_fills("BTC_TRY")

    assert len(fills) == 1
    assert fills[0].fill_id is not None
    assert len(fills[0].fill_id) == 64
    client.close()


def test_stage4_open_order_parser_handles_side_variants() -> None:
    client = BtcturkHttpClient()
    for payload in (
        {
            "id": 1,
            "pairSymbolNormalized": "BTC_TRY",
            "orderMethod": "buy",
            "price": "100",
            "quantity": "0.1",
            "status": "Untouched",
            "time": 1700000000000,
        },
        {
            "id": 2,
            "pairSymbolNormalized": "BTC_TRY",
            "method": "sell",
            "price": "100",
            "quantity": "0.1",
            "status": "Untouched",
            "time": 1700000000000,
        },
        {
            "id": 3,
            "pairSymbolNormalized": "BTC_TRY",
            "orderType": "buy",
            "price": "100",
            "quantity": "0.1",
            "status": "Untouched",
            "time": 1700000000000,
        },
    ):
        parsed = _parse_stage4_open_order_item(
            payload,
            side_parser=client._parse_side,
            status_parser=client._parse_exchange_status,
        )
        assert parsed is not None


def test_stage4_list_open_orders_requires_symbol() -> None:
    client = BtcturkHttpClientStage4(BtcturkHttpClient())
    with pytest.raises(ConfigurationError):
        client.list_open_orders()


def test_cancel_order_by_exchange_id_rejects_non_numeric() -> None:
    client = BtcturkHttpClient(api_key="k", api_secret="c2VjcmV0")
    with pytest.raises(ExchangeError):
        client.cancel_order_by_exchange_id("not-a-number")


def test_stage4_recent_fills_fallback_id_is_deterministic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/transactions/trade":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "orderId": "12",
                            "orderType": "buy",
                            "price": "100",
                            "amount": "0.1",
                            "fee": "0",
                            "feeCurrency": "TRY",
                            "timestamp": 1700000000000,
                        }
                    ],
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    base = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
        base_url="https://api.btcturk.com",
    )
    adapter = BtcturkHttpClientStage4(base)

    first = adapter.get_recent_fills("BTC_TRY")[0].fill_id
    second = adapter.get_recent_fills("BTC_TRY")[0].fill_id

    assert first == second
    assert first is not None


def test_private_request_non_429_4xx_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"success": False, "message": "bad req"})

    client = BtcturkHttpClient(
        api_key="demo-key",
        api_secret="c3VwZXItc2VjcmV0LWJ5dGVz",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ExchangeError):
        client.get_balances()

    assert calls["n"] == 1
    client.close()
