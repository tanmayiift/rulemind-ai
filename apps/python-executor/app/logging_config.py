"""Structured application logging — the thing this service did not have.

Before this module there was **no** logging configuration anywhere in ``app/``: no
``basicConfig``, no ``dictConfig``, nothing. Seven modules call
``logging.getLogger(...)`` (``db``, ``observability``, ``scheduler``, ``slo``,
``storage``, ``security_config``, and an inline one in ``main``), and every one of
those records was either dropped on the floor or emitted by ``logging.lastResort``,
which prints the bare message to stderr at WARNING with **no timestamp, no level, no
logger name**. An operator asking "why was that decision slow / why did it fail" had
literally nothing to read.

What this installs:

* one stdout handler emitting **one JSON object per line** — safe under
  ``--workers 4`` because each worker writes whole lines to its own inherited stdout,
  and no shared file is opened (a shared ``FileHandler`` across four processes is how
  you get interleaved half-records);
* ``RequestContextFilter``, which stamps every record with the current request id and
  tenant id from the ``context`` ContextVars, plus the active OpenTelemetry trace/span
  ids when tracing is on — so a log line can be joined to a span and to the request
  the caller saw;
* a level read through ``env.env_str``, because ``.env.example`` ships several
  variables written as ``NAME=`` (blank) and a blank value defeats an ``os.getenv``
  default.

``configure_logging()`` is idempotent: calling it twice (module import *and* an
explicit startup call) must not stack a second handler and double every line.
"""
from __future__ import annotations

import json
import logging
import logging.config
import sys
import threading
from typing import Any, Dict, Optional

from . import env
from .context import get_current_request_id, get_current_tenant_id

# Attributes every LogRecord carries. Anything outside this set was passed by the
# caller as `extra={...}` and is worth emitting — that is what makes the JSON line
# more useful than the message string alone.
_RESERVED = frozenset(
    (
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "taskName", "thread", "threadName",
        # injected by RequestContextFilter / opentelemetry-instrumentation-logging
        "request_id", "tenant_id", "trace_id", "span_id",
        "otelTraceID", "otelSpanID", "otelServiceName", "otelTraceSampled",
    )
)

_configure_lock = threading.Lock()
_configured = False


def _otel_ids() -> Dict[str, str]:
    """Trace/span ids of the currently-active span, or {} when tracing is off.

    Read from the live span rather than relying on ``LoggingInstrumentor`` having been
    installed, so correlation works even in a deployment that has the OTel SDK but not
    the logging instrumentation package.
    """
    try:
        from opentelemetry import trace
    except Exception:  # pragma: no cover - OTel is an optional dependency
        return {}
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is None or not ctx.is_valid:
            return {}
        return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:  # pragma: no cover - never let correlation break a log call
        return {}


class RequestContextFilter(logging.Filter):
    """Stamp request id / tenant id / trace ids onto every record.

    A ``Filter`` rather than a ``LoggerAdapter`` because it applies to records emitted
    by code that never heard of this module — SQLAlchemy, uvicorn, third-party
    libraries — which is exactly where the correlation id is most needed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = get_current_request_id()
        if not getattr(record, "tenant_id", None):
            record.tenant_id = get_current_tenant_id()
        if not getattr(record, "trace_id", None):
            # LoggingInstrumentor, when installed, has already put these on the record.
            existing_trace = getattr(record, "otelTraceID", None)
            if existing_trace and existing_trace != "0" * 32:
                record.trace_id = existing_trace
                record.span_id = getattr(record, "otelSpanID", None)
            else:
                for key, value in _otel_ids().items():
                    setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never raises — a formatter that throws loses the log
    line *and* prints a traceback to stderr, which is strictly worse than the record."""

    default_time_format = "%Y-%m-%dT%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + ".{0:03.0f}Z".format(record.msecs),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "tenant_id", "trace_id", "span_id"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_") and key not in payload:
                payload[key] = value
        try:
            return json.dumps(payload, default=str)
        except Exception:  # pragma: no cover - defensive; json.dumps(default=str) is total
            return json.dumps({"ts": payload["ts"], "level": payload["level"],
                               "logger": payload["logger"], "message": str(record.msg)})


def log_level() -> str:
    """LOG_LEVEL, blank-aware. An unrecognised value falls back to INFO rather than
    raising at startup — a typo in an env var must not take the process down."""
    raw = env.env_str("LOG_LEVEL", "INFO").upper()
    return raw if raw in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET") else "INFO"


def log_format() -> str:
    """`json` (default, for log shippers) or `text` (readable in a local terminal)."""
    raw = env.env_str("LOG_FORMAT", "json").lower()
    return raw if raw in ("json", "text") else "json"


def build_config(level: Optional[str] = None, fmt: Optional[str] = None) -> Dict[str, Any]:
    resolved_level = (level or log_level()).upper()
    resolved_fmt = (fmt or log_format()).lower()
    return {
        "version": 1,
        # False, emphatically: dictConfig's default of True disables every logger that
        # already exists, and this is called after `app.*` modules have made theirs.
        "disable_existing_loggers": False,
        "filters": {"request_context": {"()": "app.logging_config.RequestContextFilter"}},
        "formatters": {
            "json": {"()": "app.logging_config.JsonFormatter"},
            "text": {"format": "%(asctime)s %(levelname)-8s %(name)s [req=%(request_id)s] %(message)s"},
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": resolved_fmt,
                "filters": ["request_context"],
            }
        },
        "root": {"level": resolved_level, "handlers": ["stdout"]},
        "loggers": {
            # Let the servers' own records through this handler instead of their default
            # one, so access/error lines are JSON too and carry the request id.
            "uvicorn": {"level": resolved_level, "handlers": ["stdout"], "propagate": False},
            "uvicorn.error": {"level": resolved_level, "handlers": ["stdout"], "propagate": False},
            "uvicorn.access": {"level": resolved_level, "handlers": ["stdout"], "propagate": False},
            "gunicorn.error": {"level": resolved_level, "handlers": ["stdout"], "propagate": False},
            "gunicorn.access": {"level": resolved_level, "handlers": ["stdout"], "propagate": False},
            # Chatty client-side libraries: httpx logs a line per outbound request, which at
            # INFO buries our own records and, on the connector path, prints request URLs.
            # Held at WARNING unless the operator explicitly asked for DEBUG.
            **{
                name: {"level": "DEBUG" if resolved_level == "DEBUG" else "WARNING"}
                for name in ("httpx", "httpcore", "urllib3", "asyncio", "multipart")
            },
        },
    }


def configure_logging(level: Optional[str] = None, fmt: Optional[str] = None, force: bool = False) -> bool:
    """Install the structured logging configuration. Returns True if it configured now.

    Idempotent by design. It is reachable from two places (module import via
    ``observability.setup_telemetry`` and an explicit startup call), and running
    ``dictConfig`` twice would be a silent double-log of every line.
    """
    global _configured
    with _configure_lock:
        if _configured and not force:
            return False
        try:
            logging.config.dictConfig(build_config(level, fmt))
        except Exception:  # pragma: no cover - logging setup must never break boot
            # Fall back to something rather than nothing: a bare stderr handler still
            # beats lastResort, which drops everything below WARNING.
            logging.basicConfig(level=log_level(), stream=sys.stderr)
            _configured = True
            return True
        _configured = True
        return True


def reset_for_test() -> None:
    """Test hook: forget that configuration happened so the next call re-applies it."""
    global _configured
    with _configure_lock:
        _configured = False
