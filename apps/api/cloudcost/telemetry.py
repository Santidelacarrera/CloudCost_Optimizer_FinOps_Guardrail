"""OpenTelemetry opcional: se activa solo si OTEL_EXPORTER_OTLP_ENDPOINT está definido.

Lo que NUNCA debe viajar en una traza: cabeceras (`Authorization`, `Cookie`, firmas de webhook), cuerpos de petición (contraseñas, códigos
de segundo factor) ni secretos en la URL. La instrumentación de FastAPI no captura cabeceras ni cuerpos salvo que se pida con
`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`: aquí se comprueba que no esté activado, y la URL y la consulta se pasan por
`redaction` antes de salir (un `?token=…` descuidado no acabaría en el colector de trazas).
"""
from __future__ import annotations

import logging
import os

from . import redaction

log = logging.getLogger(__name__)

_URL_ATTRIBUTES = ("http.url", "http.target", "url.full", "url.query", "url.path", "http.route")
_FORBIDDEN_ENV = ("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE",
                  "OTEL_PYTHON_FASTAPI_CAPTURE_REQUEST_BODY")
EXCLUDED_URLS = "health,ready,metrics"


def _scrub_server_span(span, scope) -> None:                    # server_request_hook de la instrumentación ASGI
    attrs = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRIBUTES:
        value = attrs.get(key)
        if isinstance(value, str):
            clean = redaction.redact_text(value)
            if clean != value:
                span.set_attribute(key, clean)


def capture_settings_ok() -> list[str]:
    """Variables de entorno que activarían la captura de cabeceras o cuerpos (deben estar vacías)."""
    return [name for name in _FORBIDDEN_ENV if os.environ.get(name)]


def setup_tracing(service_name: str, endpoint: str | None, app=None, *, span_processor=None) -> None:
    if not endpoint and span_processor is None:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError:
        log.warning("OpenTelemetry no está instalado; trazas desactivadas")
        return
    if (leaks := capture_settings_ok()):
        raise RuntimeError("La captura de cabeceras/cuerpos en las trazas está activada y puede exponer secretos: " + ", ".join(leaks))
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if span_processor is None:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            log.warning("El exportador OTLP no está instalado; trazas desactivadas")
            return
        span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=(endpoint or "").rstrip("/") + "/v1/traces"))
    provider.add_span_processor(span_processor)
    trace.set_tracer_provider(provider)
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, server_request_hook=_scrub_server_span,
                                               excluded_urls=EXCLUDED_URLS)
        except ImportError:
            log.warning("opentelemetry-instrumentation-fastapi no instalado")
