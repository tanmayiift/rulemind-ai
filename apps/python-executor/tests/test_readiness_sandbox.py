"""Readiness must fail when the sandbox cannot run, not report ok and take traffic.

`/ready` checked the database and Redis and nothing else. User-authored Python variables are
what a policy is written around, so on a runtime that forbids `spawn` -- some serverless and
hardened container platforms do -- every variable fails, and the pod still answered 200. The
decisions keep coming back, because a failed variable fails CLOSED rather than open (T1.2), but
they are decided without the inputs the policy is about and nothing upstream says so. The load
balancer routes to it and the deploy looks successful.

This is the check that makes "prove the sandbox actually spawns there" answerable with a curl
rather than a guess, which is the one platform assumption a new host has to be tested for.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import sandbox  # noqa: E402


class SandboxProbeTest(unittest.TestCase):
    def test_a_working_pool_reports_ok(self):
        result = sandbox.probe()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["mode"], "pool")

    def test_inline_mode_is_ok_and_says_so(self):
        with mock.patch.dict(os.environ, {"SANDBOX_MODE": "inline"}):
            result = sandbox.probe()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["mode"], "inline")

    def test_a_runtime_that_cannot_spawn_is_an_error(self):
        """The case this exists for. A pool that cannot start must not read as healthy."""
        def _refuse(*_args, **_kwargs):
            raise OSError("[Errno 1] Operation not permitted")

        with mock.patch.dict(os.environ, {"SANDBOX_MODE": "pool", "SANDBOX_FALLBACK_MODE": "fail"}):
            with mock.patch.object(sandbox, "_get_pool", _refuse):
                result = sandbox.probe()
        self.assertEqual(result["status"], "error", result)
        self.assertIn("not permitted", result["detail"])

    def test_the_inline_fallback_is_degraded_not_ok(self):
        """It works, but untrusted customer Python is running in the API process.

        No RLIMIT, no process boundary. That is a deliberate configured trade, so it must not
        take the pod out of rotation -- and it must not read as plain "ok" either.
        """
        def _refuse(*_args, **_kwargs):
            raise OSError("pool unavailable")

        with mock.patch.dict(os.environ, {"SANDBOX_MODE": "pool", "SANDBOX_FALLBACK_MODE": "inline"}):
            with mock.patch.object(sandbox, "_get_pool", _refuse):
                result = sandbox.probe()
        self.assertEqual(result["status"], "degraded", result)
        self.assertEqual(result["mode"], "inline_fallback")


class ReadinessEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)

    def test_ready_reports_the_sandbox(self):
        body = self.client.get("/ready").json()
        self.assertIn("sandbox", body)
        self.assertEqual(body["sandbox"], "ok", body)
        self.assertEqual(body["sandbox_mode"], "pool")

    def test_ready_is_503_when_the_sandbox_cannot_run(self):
        with mock.patch.object(sandbox, "probe",
                               lambda: {"status": "error", "mode": "pool", "detail": "no spawn"}):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertFalse(body["ready"])
        self.assertEqual(body["sandbox"], "error")
        self.assertEqual(body["sandbox_detail"], "no spawn")

    def test_the_inline_fallback_stays_in_rotation_but_is_not_ok(self):
        with mock.patch.object(sandbox, "probe",
                               lambda: {"status": "degraded", "mode": "inline_fallback",
                                        "detail": "pool down"}):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200, "a configured fallback must not drain the pod")
        body = response.json()
        self.assertTrue(body["ready"])
        self.assertEqual(body["status"], "degraded")

    def test_ready_is_503_when_the_database_is_unreachable(self):
        """The probe's entire job, and it had never once done it.

        `_dependency_status` used to write `with storage.connect(): db_status = "ok"`, which
        looks like a connectivity check and is not: `SessionLocal()` builds a Session LAZILY --
        no DBAPI connection until a statement runs -- and the commit on an unused session is a
        no-op. With Postgres stopped it still reported "ok" and answered 200, so a pod with no
        database stayed in rotation. Found by scripts/smoke_compose.sh, which stops the
        container and re-probes; nothing else could have found it, because on SQLite the
        "connection" is a local file that is always there.
        """
        # The fake must CONSTRUCT fine and fail on execute(), because that is what a stopped
        # database looks like from here -- SessionLocal() never touches the server. A fake that
        # raises on construction is caught by the old lazy check too, so it proves nothing.
        class _SessionThatCannotReachTheServer:
            def execute(self, *_args, **_kwargs):
                raise OSError("connection refused")

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        original = app_main.storage.SessionLocal
        app_main.storage.SessionLocal = _SessionThatCannotReachTheServer
        try:
            response = self.client.get("/ready")
        finally:
            app_main.storage.SessionLocal = original

        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertFalse(body["ready"])
        self.assertEqual(body["db"], "error")

    def test_liveness_still_does_no_io(self):
        """/health must not gain the sandbox probe: a liveness probe that spawns a process on
        every scrape restarts the fleet the moment the pool is briefly recycling."""
        with mock.patch.object(sandbox, "probe",
                               lambda: {"status": "error", "mode": "pool", "detail": "down"}):
            self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
