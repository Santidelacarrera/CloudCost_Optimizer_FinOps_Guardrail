"""OpenTelemetry opcional: se activa solo si OTEL_EXPORTER_OTLP_ENDPOINT está definido."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_tracing(service_name: str, endpoint: str | None, app=None) -> None:
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OpenTelemetry no está instalado; trazas desactivadas")
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app)
        except ImportError:
            log.warning("opentelemetry-instrumentation-fastapi no instalado")
