from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from btcbot.adapters.exchange_stage4 import OrderAck
from btcbot.domain.models import ExchangeError, Order, OrderSide, OrderStatus
from btcbot.services import execution_wrapper as wrapper_module
from btcbot.services.execution_wrapper import ExecutionWrapper, UncertainResult


@dataclass
class _CounterEvent:
    name: str
    value: int
    attrs: dict[str, object] | None


class _FakeInstrumentation:
    def __init__(self) -> None:
        self.events: list[_CounterEvent] = []

    def counter(self, name: str, value: int = 1, *, attrs=None) -> None:
        self.events.append(_CounterEvent(name=name, value=value, attrs=attrs))


class _Stage3Exchange:
    def __init__(self, submit_error: Exception | None = None) -> None:
        self.submit_error = submit_error
        self.submit_calls = 0

    def place_limit_order(self, **kwargs):
        self.submit_calls += 1
        if self.submit_error is not None:
            raise self.submit_error
        return Order(
            order_id="ok-1",
            symbol=str(kwargs["symbol"]),
            side=kwargs["side"],
            price=kwargs["price"],
            quantity=kwargs["quantity"],
            status=OrderStatus.OPEN,
        )

    def cancel_order(self, order_id: str) -> bool:
        del order_id
        return True


class _Stage4Exchange:
    def __init__(self, submit_error: Exception | None = None) -> None:
        self.submit_error = submit_error
        self.submit_calls = 0

    def submit_limit_order(self, **kwargs):
        self.submit_calls += 1
        if self.submit_error is not None:
            raise self.submit_error
        return OrderAck(exchange_order_id=f"ex-{kwargs['client_order_id']}", status="open")

    def cancel_order_by_exchange_id(self, exchange_order_id: str) -> bool:
        del exchange_order_id
        return True


@pytest.mark.parametrize(
    ("error", "kind", "retry_calls"),
    [
        (ExchangeError("rl", status_code=429), "raise", 3),
        (ExchangeError("transient", status_code=503), "raise", 3),
        (ExchangeError("auth", status_code=401), "raise", 1),
        (ExchangeError("reject", status_code=400), "raise", 1),
        (TimeoutError("uncertain"), "uncertain", 1),
    ],
)
def test_wrapper_parity_stage3_stage4(error, kind, retry_calls) -> None:
    stage3 = ExecutionWrapper(
        _Stage3Exchange(submit_error=error), submit_retry_max_attempts=3, sleep_fn=lambda _: None
    )
    stage4 = ExecutionWrapper(
        _Stage4Exchange(submit_error=error), submit_retry_max_attempts=3, sleep_fn=lambda _: None
    )

    for wrapper, kwargs in (
        (
            stage3,
            {
                "symbol": "BTCTRY",
                "side": OrderSide.BUY,
                "price": Decimal("1"),
                "quantity": Decimal("1"),
                "client_order_id": "cid",
            },
        ),
        (
            stage4,
            {
                "symbol": "BTCTRY",
                "side": "buy",
                "price": Decimal("1"),
                "qty": Decimal("1"),
                "client_order_id": "cid",
            },
        ),
    ):
        if kind == "raise":
            with pytest.raises(type(error)):
                wrapper.submit_limit_order(**kwargs)
        else:
            result = wrapper.submit_limit_order(**kwargs)
            assert isinstance(result, UncertainResult)

    assert stage3.exchange.submit_calls == retry_calls
    assert stage4.exchange.submit_calls == retry_calls


def test_wrapper_metrics_attempts_and_uncertain(monkeypatch) -> None:
    fake_metrics = _FakeInstrumentation()
    monkeypatch.setattr(wrapper_module, "get_instrumentation", lambda: fake_metrics)
    wrapper = ExecutionWrapper(
        _Stage3Exchange(submit_error=TimeoutError("uncertain")), sleep_fn=lambda _: None
    )

    result = wrapper.submit_limit_order(
        symbol="BTCTRY",
        side=OrderSide.BUY,
        price=Decimal("1"),
        quantity=Decimal("1"),
        client_order_id="cid",
    )

    assert isinstance(result, UncertainResult)
    assert any(event.name == "execution_attempts_total" for event in fake_metrics.events)
    assert any(event.name == "execution_uncertain_total" for event in fake_metrics.events)
