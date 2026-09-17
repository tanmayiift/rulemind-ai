"""Liveness must not depend on Postgres, and blank env vars must not kill the process (4.1/4.3/4.9).

Three failures that share one property: they are invisible in local development and only fire
on a real deployment, because they are triggered by how a Helm chart renders config and by how
an orchestrator probes a pod.

**/health was a dependency check wired to the LIVENESS probe.** It called `_dependency_status()`
and answered 503 whenever Postgres was unreachable. Liveness failing means "restart this
container" -- so a 30-second database failover, which every pod survives, instead restarted the
entire API fleet at once, and they all came back cold against the database that had just
recovered. Readiness failing means "take this pod out of rotation", which is self-healing and
needs no restart; that is where a dependency check belongs, and /ready already had one. It was
additionally a sync `def`, sharing FastAPI's bounded threadpool with /decide, so a saturated
sandbox pool could time the probe out and trigger the same mass restart with every dependency
perfectly healthy.

**`os.getenv(name, default)` returns "" for a set-but-blank variable**, not the default -- and a
Helm chart renders exactly that for an unset override. The failure direction differs per call
site, which is why they need separate assertions:

    CORS_ORIGINS=""            -> zero allowed origins -> every browser request blocked, at
                                  runtime, looking like a frontend bug
    SDK_DECISIONS_BATCH_MAX="" -> int("") raises ValueError at MODULE IMPORT, before a single
                                  route is registered, so the process never starts at all

**`admin_cookie_secure()` keyed off NODE_ENV and defaulted to development**, which meant the
admin-JWT cookie shipped WITHOUT `Secure` on any deployment that did not happen to set NODE_ENV
in the API container -- i.e. all of them. It was a byte-identical duplicate of
`session_cookie._secure()`, and `runtime.is_local_dev()` carries a comment about a previous
copy of this same helper drifting after a fix landed in only one of them.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterator
from contextlib import contextmanager
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.env import env_int, env_str  # noqa: E402
from app.middleware import admin_cookie_secure  # noqa: E402
from app.storage import Storage  # noqa: E402


@contextmanager
def deployed_env(**values: str) -> Iterator[None]:
    """Env as a real deployment sees it. `PYTEST_CURRENT_TEST` is dropped because
    security_config._is_local_dev short-circuits on it, which would mask the very thing under
    test here; patch.dict restores the whole mapping, popped keys included."""
    with mock.patch.dict(os.environ, values, clear=False):
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        yield


class LivenessIsIndependentOfDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "probe.db"))
        self.client = TestClient(app_main.app)

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

        # Restore the global, and release this test's engine. Placed at the END of tearDown so
        # the teardown above still runs against THIS test's storage -- an earlier version put it
        # first and silently redirected those statements at the restored global.
        #
        # The restore is the load-bearing half. Leaving app_main.storage pointed at a deleted
        # tempdir made an UNRELATED module later fail with
        # `sqlite3.OperationalError: unable to open database file`: the failure lands on whoever
        # reads the global next, never on the test that leaked it, which is why it looked like a
        # random flake for so long.
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        try:
            app_main.storage = self._orig_storage
        except Exception:  # pragma: no cover
            pass

    def test_health_stays_200_when_the_database_is_unreachable(self):
        """The whole point. With helm's livenessProbe on /health, a 503 here restarts every
        replica simultaneously during a failover the pods would otherwise have ridden out."""
        with mock.patch.object(app_main.storage, "connect", side_effect=RuntimeError("db down")):
            response = self.client.get("/health")
            aliased = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(aliased.status_code, 200, aliased.text)
        self.assertEqual(response.json()["status"], "ok")

    def test_health_does_not_touch_the_database_at_all(self):
        """Not merely tolerant of a failure -- it must not issue the query. A probe that opens
        a connection per call also adds pool pressure exactly when the pool is the problem."""
        with mock.patch.object(app_main.storage, "connect") as connect:
            self.client.get("/health")
        connect.assert_not_called()

    def test_health_runs_on_the_event_loop_not_the_request_threadpool(self):
        """A sync `def` endpoint is dispatched to the same bounded threadpool as /decide, so a
        saturated sandbox pool could fail liveness with every dependency healthy."""
        self.assertTrue(asyncio.iscoroutinefunction(app_main.health),
                        "/health must be `async def` to stay off the threadpool")

    def test_readiness_still_fails_closed_on_a_dead_database(self):
        """The dependency check is not deleted, it is moved. A readiness failure drains the
        pod from the load balancer and reverses itself when the dependency returns."""
        with mock.patch.object(app_main.storage, "connect", side_effect=RuntimeError("db down")):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertFalse(response.json()["ready"])

    def test_readiness_reports_healthy_normally(self):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ready"])
        # /health/ready is the path k8s/rulemind.yaml probes; both aliases must survive.
        self.assertEqual(self.client.get("/health/ready").status_code, 200)


class BlankEnvironmentVariableTests(unittest.TestCase):
    def test_blank_cors_origins_falls_back_instead_of_blocking_every_origin(self):
        with mock.patch.dict(os.environ, {"CORS_ORIGINS": ""}):
            origins = app_main.parse_origins()
        self.assertTrue(origins, "a blank CORS_ORIGINS produced ZERO allowed origins")
        self.assertIn("http://localhost:3000", origins)

    def test_the_module_still_imports_with_blank_int_config(self):
        """The dangerous class: an `int(os.getenv(...))` at module scope raises before a single
        route is registered, so the process never starts and nothing in the app gets to log why.

        Checked in a subprocess because the constant is bound at import and this process has
        already imported the module -- an in-process assertion could not observe the crash.
        """
        with self.assertRaises(ValueError):
            int("")  # what the old expression did with a set-but-blank value
        env = {**os.environ, "SDK_DECISIONS_BATCH_MAX": "", "PYTHON_SANDBOX_TIMEOUT": "",
               "CORS_ORIGINS": "", "RULEMIND_CONFIG_KEY": "rulemind-test-key"}
        probe = subprocess.run(
            [sys.executable, "-c",
             "import app.main as m; assert m.SDK_DECISIONS_BATCH_MAX == 1000, m.SDK_DECISIONS_BATCH_MAX"],
            cwd=str(APP_ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr[-2000:])
        self.assertEqual(env_int("SDK_DECISIONS_BATCH_MAX", 1000), 1000)

    def test_blank_sandbox_limits_fall_back_to_the_documented_defaults(self):
        """`get_settings` is stubbed EMPTY on purpose. A seeded workspace always carries
        engine_config.timeout_ms, so the env fallback is only reached by a workspace whose
        settings do not define one — which is exactly the deployment that would then crash on
        `int("")` in every decision, and the case a stored-value test cannot reach."""
        empty: Dict[str, Any] = {"engine_config": {}}
        with mock.patch.object(app_main.storage, "get_settings", return_value=empty):
            with mock.patch.dict(os.environ, {"PYTHON_SANDBOX_TIMEOUT": "", "PYTHON_SANDBOX_MEMORY": ""}):
                limits = app_main.engine_limits()
        self.assertEqual(limits, {"timeout_ms": 2000, "memory_mb": 128})

    def test_a_blank_stored_override_also_falls_back(self):
        settings: Dict[str, Any] = {"engine_config": {"timeout_ms": "", "memory_mb": None}}
        with mock.patch.object(app_main.storage, "get_settings", return_value=settings):
            with mock.patch.dict(os.environ, {"PYTHON_SANDBOX_TIMEOUT": "3000"}):
                limits = app_main.engine_limits()
        self.assertEqual(limits, {"timeout_ms": 3000, "memory_mb": 128})

    def test_a_stored_engine_override_still_wins(self):
        settings: Dict[str, Any] = {"engine_config": {"timeout_ms": 5000, "memory_mb": 256}}
        with mock.patch.object(app_main.storage, "get_settings", return_value=settings):
            limits = app_main.engine_limits()
        self.assertEqual(limits, {"timeout_ms": 5000, "memory_mb": 256})

    def test_blank_redis_url_reads_as_not_configured_not_as_an_error(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": ""}):
            self.assertEqual(env_str("REDIS_URL"), "")
            self.assertEqual(app_main._dependency_status()["redis"], "not_configured")


class AdminCookieSecureTests(unittest.TestCase):
    def test_production_sets_secure_even_when_node_env_says_development(self):
        """The exact live configuration: the manifests set RULEMIND_ENV=production and nothing
        sets NODE_ENV in the API container, so the old NODE_ENV-keyed helper returned False and
        the admin session cookie went out over the wire without `Secure`."""
        with deployed_env(RULEMIND_ENV="production", NODE_ENV="development"):
            self.assertTrue(admin_cookie_secure())

    def test_staging_counts_as_deployed(self):
        with deployed_env(RULEMIND_ENV="staging"):
            self.assertTrue(admin_cookie_secure())

    def test_local_development_still_works_over_plain_http(self):
        with deployed_env(RULEMIND_ENV="development"):
            self.assertFalse(admin_cookie_secure())

    def test_it_delegates_rather_than_duplicating(self):
        """The duplicate is the bug: `runtime.is_local_dev` carries a comment about a previous
        byte-identical copy of this helper drifting after a fix landed in only one place."""
        source = inspect.getsource(admin_cookie_secure)
        self.assertIn("is_local_dev", source)
        self.assertNotIn("NODE_ENV", source.split('"""')[-1],
                         "admin_cookie_secure must not read NODE_ENV itself")


if __name__ == "__main__":
    unittest.main()
