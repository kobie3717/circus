"""OpenTelemetry instrumentation for The Circus."""

import os
from typing import Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from circus.config import settings


def setup_tracing(app):
    """Configure OpenTelemetry tracing."""
    # Create tracer provider
    resource = Resource(attributes={
        SERVICE_NAME: "circus-api",
        "service.version": settings.app_version,
    })

    provider = TracerProvider(resource=resource)

    # Add span processors
    if settings.debug:
        # Console exporter for development
        provider.add_span_processor(
            BatchSpanProcessor(ConsoleSpanExporter())
        )
    else:
        # OTLP exporter for production (Jaeger, Tempo, etc.)
        # Requires OTEL_EXPORTER_OTLP_ENDPOINT env var
        otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otlp_endpoint:
            try:
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter())
                )
            except Exception:
                # Fallback to console if OTLP fails
                provider.add_span_processor(
                    BatchSpanProcessor(ConsoleSpanExporter())
                )
        else:
            # No OTLP endpoint configured, use console
            provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter())
            )

    trace.set_tracer_provider(provider)

    # Instrument FastAPI
    FastAPIInstrumentor.instrument_app(app)

    return provider


def get_current_trace_id() -> Optional[str]:
    """Get current trace ID as hex string."""
    span = trace.get_current_span()
    if span and span.get_span_context().is_valid:
        return format(span.get_span_context().trace_id, '032x')
    return None
