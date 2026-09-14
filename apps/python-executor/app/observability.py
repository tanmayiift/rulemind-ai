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

from .logging_config import configure_logging
from .metrics_registry import prepare_multiproc_dir

logger = logging.getLogger("rulemind.observability")


def setup_telemetry(app: Any, engine: Optional[Any] = None) -> bool:
    """Configure structured logging, then instrument the app for OTLP tracing.

    Returns True when *tracing* was activated, False when skipped.

    Logging is configured here, unconditionally and before the OTLP check, because this
    is the one observability hook ``main`` already calls at import time — and logging
    must not be conditional on an OTLP endpoint being configured. It is idempotent, so
    an explicit ``configure_logging()`` at startup remains the preferred wiring and
    calling both is harmless.
    """
    configure_logging()
    # Make the Prometheus multiprocess directory exist before any collector is constructed.
    # prometheus_client opens `<dir>/<type>_<pid>.db` lazily on the first metric and raises if
    # the directory is missing. Creating (never clearing — that would wipe sibling workers'
    # counters) is safe to do per worker; the entrypoint still owns wiping it before the fork.
    prepare_multiproc_dir()
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
    # Correlate logs to spans. Without this a trace shows a slow decision but the log
    # lines from that decision carry no trace id, so there is no way to get from the
    # span to the reason. `set_logging_format=False` because this module already owns
    # the format — letting the instrumentor call basicConfig() would replace the JSON
    # handler with its own plain-text one.
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=False)
    except ImportError:
        # Not installed: RequestContextFilter reads the active span directly, so log
        # correlation still works — this instrumentor only adds it for records made
        # outside our filter's reach.
        logger.info("opentelemetry-instrumentation-logging not installed; "
                    "using the in-process span lookup for log/trace correlation.")
    except Exception as exc:  # pragma: no cover - optional dependency path
        logger.warning("Logging instrumentation skipped: %s", exc)
    if engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=engine)
        except Exception as exc:  # pragma: no cover - optional dependency path
            logger.warning("SQLAlchemy instrumentation skipped: %s", exc)
    logger.info("OpenTelemetry tracing enabled → %s (service=%s)", endpoint, service_name)
    return True
