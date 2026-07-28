"""OpenTelemetry setup — optional and non-fatal.

Traces are exported via OTLP when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (e.g.
``http://otel-collector:4317``); otherwise this is a no-op so local/dev and tests
run untouched. Metrics continue to be served on the existing Prometheus
``/metrics`` endpoint (scraped by Prometheus → Grafana). Point any OTLP backend
(Grafana Tempo is the default in docker-compose) at the collector and you get
distributed traces of every decision request with zero code changes.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("rulemind.observability")


def setup_telemetry(app: Any, engine: Optional[Any] = None) -> bool:
    """Instrument the FastAPI app (and SQLAlchemy engine) for OTLP tracing.

    Returns True when instrumentation was activated, False when skipped.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OTEL endpoint set but opentelemetry libraries are not installed; skipping tracing.")
        return False

    service_name = os.getenv("OTEL_SERVICE_NAME", "rulemind-api")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    if engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=engine)
        except Exception as exc:  # pragma: no cover - optional dependency path
            logger.warning("SQLAlchemy instrumentation skipped: %s", exc)
    logger.info("OpenTelemetry tracing enabled → %s (service=%s)", endpoint, service_name)
    return True
