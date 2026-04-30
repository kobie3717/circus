"""OpenTelemetry instrumentation for The Circus."""

import os
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider

from circus.config import settings


def setup_tracing(app):
    """Configure OpenTelemetry tracing.

    No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
    OTLP/gRPC and FastAPI instrumentation imports are lazy to avoid
    loading heavy gRPC libs (~700MB) on every startup.
    """
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    if not otlp_endpoint and not settings.debug:
        # No tracing configured — return bare provider (no-op)
        return TracerProvider()

    # Heavy imports only when actually needed
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    resource = Resource(attributes={
        SERVICE_NAME: "circus-api",
        "service.version": settings.app_version,
    })
    provider = TracerProvider(resource=resource)

    if settings.debug or not otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except Exception:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    return provider


def get_current_trace_id() -> Optional[str]:
    """Get current trace ID as hex string."""
    span = trace.get_current_span()
    if span and span.get_span_context().is_valid:
        return format(span.get_span_context().trace_id, '032x')
    return None
