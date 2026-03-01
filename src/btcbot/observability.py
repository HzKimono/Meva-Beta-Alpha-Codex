from __future__ import annotations

import atexit
import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
_METRIC_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _sanitize_metric_name(name: str) -> str:
    return _METRIC_NAME_RE.sub("_", name).strip("_") or "invalid_metric"


@dataclass(frozen=True)
class CorrelationContext:
    run_id: str | None = None
    cycle_id: str | None = None
    client_order_id: str | None = None
    order_id: str | None = None
    symbol: str | None = None

    def as_attributes(self) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for key, value in self.__dict__.items():
            if value:
                attrs[key] = value
        return attrs


class Instrumentation:
    def counter(self, name: str, value: int = 1, *, attrs: dict[str, Any] | None = None) -> None:
        return None

    def gauge(self, name: str, value: float, *, attrs: dict[str, Any] | None = None) -> None:
        return None

    def histogram(self, name: str, value: float, *, attrs: dict[str, Any] | None = None) -> None:
        return None

    @contextmanager
    def trace(self, name: str, *, attrs: dict[str, Any] | None = None) -> Iterator[None]:
        del name, attrs
        yield

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class NoopInstrumentation(Instrumentation):
    pass


class OTelInstrumentation(Instrumentation):
    def __init__(
        self,
        *,
        service_name: str,
        metrics_exporter: str,
        otlp_endpoint: str | None,
        prometheus_port: int,
    ) -> None:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": service_name})

        span_exporter = (
            OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter()
        )
        self._trace_provider = TracerProvider(resource=resource)
        self._trace_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(self._trace_provider)
        self._tracer = trace.get_tracer(service_name)

        metric_readers = []
        if metrics_exporter == "otlp":
            metric_exporter = (
                OTLPMetricExporter(endpoint=otlp_endpoint)
                if otlp_endpoint
                else OTLPMetricExporter()
            )
            metric_readers.append(PeriodicExportingMetricReader(metric_exporter))
        elif metrics_exporter == "prometheus":
            from opentelemetry.exporter.prometheus import PrometheusMetricReader
            from prometheus_client import start_http_server

            metric_readers.append(PrometheusMetricReader())
            start_http_server(prometheus_port)

        self._metric_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
        metrics.set_meter_provider(self._metric_provider)
        meter = metrics.get_meter(service_name)

        self._counters: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._meter = meter

    def counter(self, name: str, value: int = 1, *, attrs: dict[str, Any] | None = None) -> None:
        safe_name = _sanitize_metric_name(name)
        counter = self._counters.get(safe_name)
        if counter is None:
            counter = self._meter.create_counter(safe_name)
            self._counters[safe_name] = counter
        counter.add(value, attrs or {})

    def gauge(self, name: str, value: float, *, attrs: dict[str, Any] | None = None) -> None:
        # Use up/down counter as a portable synchronous gauge surrogate.
        safe_name = _sanitize_metric_name(name)
        gauge = self._gauges.get(safe_name)
        if gauge is None:
            gauge = self._meter.create_up_down_counter(safe_name)
            self._gauges[safe_name] = gauge
        gauge.add(value, attrs or {})

    def histogram(self, name: str, value: float, *, attrs: dict[str, Any] | None = None) -> None:
        safe_name = _sanitize_metric_name(name)
        histogram = self._histograms.get(safe_name)
        if histogram is None:
            histogram = self._meter.create_histogram(safe_name)
            self._histograms[safe_name] = histogram
        histogram.record(value, attrs or {})

    @contextmanager
    def trace(self, name: str, *, attrs: dict[str, Any] | None = None) -> Iterator[None]:
        with self._tracer.start_as_current_span(name) as span:
            for key, value in (attrs or {}).items():
                span.set_attribute(key, value)
            yield

    def flush(self) -> None:
        self._metric_provider.force_flush()
        self._trace_provider.force_flush()

    def shutdown(self) -> None:
        self.flush()
        self._metric_provider.shutdown()
        self._trace_provider.shutdown()


_LOCK = threading.Lock()
_INSTRUMENTATION: Instrumentation = NoopInstrumentation()
_CONFIGURED_ONCE = False


def configure_instrumentation(
    *,
    enabled: bool,
    service_name: str = "btcbot",
    metrics_exporter: str = "none",
    otlp_endpoint: str | None = None,
    prometheus_port: int = 9464,
) -> Instrumentation:
    global _INSTRUMENTATION, _CONFIGURED_ONCE
    with _LOCK:
        if _CONFIGURED_ONCE:
            return _INSTRUMENTATION
        _CONFIGURED_ONCE = True
        if not enabled:
            _INSTRUMENTATION = NoopInstrumentation()
            return _INSTRUMENTATION
        try:
            _INSTRUMENTATION = OTelInstrumentation(
                service_name=service_name,
                metrics_exporter=metrics_exporter,
                otlp_endpoint=otlp_endpoint,
                prometheus_port=prometheus_port,
            )
        except Exception:  # noqa: BLE001
            logger.exception("observability_setup_failed_falling_back_to_noop")
            _INSTRUMENTATION = NoopInstrumentation()
        return _INSTRUMENTATION


def get_instrumentation() -> Instrumentation:
    return _INSTRUMENTATION


def flush_instrumentation() -> None:
    _INSTRUMENTATION.flush()


def shutdown_instrumentation() -> None:
    _INSTRUMENTATION.shutdown()


atexit.register(shutdown_instrumentation)
