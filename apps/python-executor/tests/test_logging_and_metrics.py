"""There was no logging configuration at all, and /metrics was per-process (tasks 5.1, 5.2).

**Logging.** Not "the format was poor" -- there was nothing. No ``basicConfig``, no
``dictConfig``, no ``logging.config`` anywhere under ``app/`` (the only hit in the tree is
Alembic's own ``env.py``). Seven modules call ``logging.getLogger`` -- ``db``,
``observability``, ``scheduler``, ``slo``, ``storage``, ``security_config``, plus an inline
one in ``main`` -- and with no handler configured Python's ``lastResort`` handler takes over:
it prints the bare message to stderr at WARNING and above, with no timestamp, no level, no
logger name, and it drops INFO and DEBUG entirely. So the WAL-recovery warning, the SLO
breach, the decryption error and the "decision computed but NOT persisted" alarm were all
either invisible or an unattributed line of text. An operator asking "why was that decision
slow, or why did it fail" had nothing to read.

There was also no request-ID propagation anywhere -- ``X-Request-Id`` had zero hits in the
repo -- so even once records had timestamps there was no way to group the ones belonging to
one caller's request across four workers.

**Metrics.** ``PROMETHEUS_MULTIPROC_DIR`` appeared nowhere, and the Dockerfile runs uvicorn
with ``--workers 4``. Four processes each keep their own counters and a scrape hits one of
them at random, so ``rulemind_decisions_total`` appears to jump around and go *backwards*
between scrapes -- which makes ``rate()`` over it, the one query anybody actually writes,
meaningless. And ``decision_log`` poked ``REGISTRY._names_to_collectors`` directly for
reload-safety, a private attribute whose absence would be a hard "Duplicated timeseries" crash
rather than a fallback.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from prometheus_client import Counter  # noqa: E402

import app.logging_config as logging_config  # noqa: E402
import app.metrics_registry as metrics_registry  # noqa: E402
from app.context import request_id_scope, sanitize_request_id  # noqa: E402


def _capture(record_fn, level="INFO"):
    """Run `record_fn` against a fresh JSON handler and return the parsed lines."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_config.JsonFormatter())
    handler.addFilter(logging_config.RequestContextFilter())
    logger = logging.getLogger("rulemind.test.capture")
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False
    try:
        record_fn(logger)
    finally:
        logger.handlers = []
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class StructuredLoggingTests(unittest.TestCase):
    def test_a_record_carries_timestamp_level_and_logger(self):
        """lastResort gave none of these -- just the message text on stderr."""
        (line,) = _capture(lambda log: log.warning("policy %s was slow", "p1"))
        self.assertEqual(line["level"], "WARNING")
        self.assertEqual(line["logger"], "rulemind.test.capture")
        self.assertEqual(line["message"], "policy p1 was slow")
        self.assertRegex(line["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_extras_are_emitted_not_discarded(self):
        """The reason a structured line beats a formatted string: the fields stay queryable."""
        (line,) = _capture(lambda log: log.info(
            "decision", extra={"policy_id": "p1", "latency_ms": 912}))
        self.assertEqual(line["policy_id"], "p1")
        self.assertEqual(line["latency_ms"], 912)

    def test_info_records_are_not_dropped(self):
        """lastResort's level is WARNING, so every INFO record in the app was silently lost."""
        lines = _capture(lambda log: log.info("scheduler tick"))
        self.assertEqual(len(lines), 1)

    def test_the_request_id_is_stamped_on_every_record(self):
        """X-Request-Id had zero hits in the repo. Without it, four workers' interleaved
        records cannot be grouped back into the request one caller actually made."""
        lines = _capture(_with_scope)
        self.assertEqual(len(lines), 2)
        self.assertEqual({line["request_id"] for line in lines}, {"req-abc"})

    def test_a_record_outside_a_request_carries_no_request_id(self):
        (line,) = _capture(lambda log: log.info("startup"))
        self.assertNotIn("request_id", line)

    def test_an_exception_is_serialised_not_lost(self):
        def emit(log):
            try:
                raise ValueError("bad connector config")
            except ValueError:
                log.error("connector failed", exc_info=True)

        (line,) = _capture(emit)
        self.assertIn("ValueError: bad connector config", line["exception"])

    def test_the_formatter_never_raises_on_an_unserialisable_extra(self):
        (line,) = _capture(lambda log: log.info("obj", extra={"thing": object()}))
        self.assertIn("thing", line)

    def test_an_inbound_request_id_is_sanitised(self):
        """The id is attacker-controlled and lands in every log line for the request.
        Unrestricted, a newline in it forges whole log records; unbounded length is a
        second, cheaper denial-of-service on the log pipeline."""
        self.assertIsNone(sanitize_request_id("evil\nlevel=ERROR message=forged"))
        self.assertIsNone(sanitize_request_id("x" * 500))
        self.assertIsNone(sanitize_request_id(""))
        self.assertEqual(sanitize_request_id("  abc-123_9  "), "abc-123_9")

    def test_a_rejected_inbound_id_is_replaced_not_dropped(self):
        with request_id_scope("evil\ninjected") as resolved:
            self.assertNotIn("\n", resolved)
            self.assertTrue(resolved)

    def test_configure_logging_is_idempotent(self):
        """It is reachable from module import AND from an explicit startup call, and under
        --workers 4 from four processes. Stacking a second handler double-logs every line."""
        logging_config.reset_for_test()
        self.assertTrue(logging_config.configure_logging())
        first = len(logging.getLogger().handlers)
        self.assertFalse(logging_config.configure_logging(), "second call reconfigured")
        self.assertTrue(logging_config.configure_logging(force=True))
        self.assertEqual(len(logging.getLogger().handlers), first,
                         "handlers stacked -- every record would be emitted twice")

    def test_level_and_format_are_blank_aware(self):
        """`.env.example` ships variables written as `NAME=`, and a blank value defeats an
        os.getenv default -- the failure mode app/env.py exists for."""
        for key, reader, expected in (("LOG_LEVEL", logging_config.log_level, "INFO"),
                                      ("LOG_FORMAT", logging_config.log_format, "json")):
            saved = os.environ.get(key)
            try:
                os.environ[key] = ""
                self.assertEqual(reader(), expected)
                os.environ[key] = "not-a-real-value"
                self.assertEqual(reader(), expected, "a typo must not take the process down")
            finally:
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved

    def test_the_config_does_not_disable_existing_loggers(self):
        """dictConfig defaults `disable_existing_loggers` to True, and this runs after every
        `app.*` module has already created its logger -- which would silence all of them."""
        self.assertFalse(logging_config.build_config()["disable_existing_loggers"])


def _with_scope(log):
    with request_id_scope("req-abc"):
        log.info("first")
        log.info("second")


class MetricsRegistryTests(unittest.TestCase):
    def test_get_or_create_is_reload_safe_for_a_counter(self):
        """A Counter registers under both `name` and `name_total`; missing that is a hard
        'Duplicated timeseries' crash on module reload, not a degraded metric."""
        name = "rulemind_test_metrics_registry_counter"
        first = metrics_registry.get_or_create(Counter, name, "doc", ("source",))
        second = metrics_registry.get_or_create(Counter, name, "doc", ("source",))
        self.assertIs(first, second)

    def test_get_or_create_falls_back_on_the_duplicate_registration_error(self):
        """The old inline poke read REGISTRY._names_to_collectors and nothing else, so a miss
        there -- a renamed private attribute, or a name spelling it does not check -- was a
        hard 'Duplicated timeseries in CollectorRegistry' crash at import. The registration
        error is the second, version-independent way to learn the collector already exists."""
        name = "rulemind_test_metrics_registry_fallback"
        original = metrics_registry.get_or_create(Counter, name, "doc", ())
        real_find = metrics_registry._find_existing
        calls = {"n": 0}

        def blind_first(metric_name):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_find(metric_name)

        with mock.patch.object(metrics_registry, "_find_existing", side_effect=blind_first):
            again = metrics_registry.get_or_create(Counter, name, "doc", ())
        self.assertIs(again, original)
        self.assertEqual(calls["n"], 2, "the ValueError fallback path was not taken")

    def test_multiprocess_is_off_unless_the_directory_is_configured(self):
        saved = os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
        try:
            os.environ["PROMETHEUS_MULTIPROC_DIR"] = ""  # blank must read as unset
            self.assertFalse(metrics_registry.is_multiprocess())
        finally:
            if saved is not None:
                os.environ["PROMETHEUS_MULTIPROC_DIR"] = saved
            else:
                os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)

    def test_a_scrape_sums_every_workers_counter(self):
        """The actual bug: with --workers 4 a scrape hits one worker at random, so a counter
        appears to move backwards and rate() over it is garbage."""
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            saved = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
            os.environ["PROMETHEUS_MULTIPROC_DIR"] = tmp
            try:
                from prometheus_client import CollectorRegistry, multiprocess, values

                # Write two workers' counter files the way four uvicorn workers would.
                for pid, amount in ((11111, 3.0), (22222, 4.0)):
                    value_class = values.MultiProcessValue(process_identifier=lambda p=pid: p)
                    counter = value_class("counter", "rulemind_multiproc_probe_total",
                                          "rulemind_multiproc_probe_total", (), (), "doc")
                    counter.inc(amount)

                registry = CollectorRegistry()
                multiprocess.MultiProcessCollector(registry, path=tmp)
                total = registry.get_sample_value("rulemind_multiproc_probe_total")
                self.assertEqual(total, 7.0,
                                 "the scrape returned one worker's value instead of the sum")
                self.assertTrue(metrics_registry.is_multiprocess())
                self.assertIsNot(metrics_registry.build_registry(),
                                 __import__("prometheus_client").REGISTRY,
                                 "build_registry() must return a multiprocess registry here")
            finally:
                if saved is not None:
                    os.environ["PROMETHEUS_MULTIPROC_DIR"] = saved
                else:
                    os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)

    def test_preparing_the_directory_never_wipes_it(self):
        """It runs once per worker. A worker that cleared the directory would destroy the
        counters its three siblings are writing -- the same shape as the WAL delete bug."""
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            marker = os.path.join(tmp, "counter_11111.db")
            Path(marker).write_bytes(b"x")
            saved = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
            os.environ["PROMETHEUS_MULTIPROC_DIR"] = tmp
            try:
                metrics_registry.prepare_multiproc_dir()
                self.assertTrue(os.path.exists(marker))
            finally:
                if saved is not None:
                    os.environ["PROMETHEUS_MULTIPROC_DIR"] = saved
                else:
                    os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)


if __name__ == "__main__":
    unittest.main()
