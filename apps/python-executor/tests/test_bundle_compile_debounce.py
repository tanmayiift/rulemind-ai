"""A change inside the compile debounce window must still reach the bundle.

`mark_bundle_compile_queued` debounces compiles to one per 5 s per tenant -- and a change inside the
window was DROPPED, not deferred: nothing was scheduled, so the bundle stayed at the previous change
until some later, unrelated write. Found building the Decision Explorer's replay (plan 6.5): a
policy changed within seconds of its creation replayed against a bundle that did not contain the
change. The bundle is what SDKs download and what replay, what-if and backtest read.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

WINDOW = 0.4


def _policy(outcome: str) -> dict:
    return {"id": "debounce_probe", "name": "probe", "status": "prod", "defaultOutcome": "review",
            "steps": [{"id": "s", "type": "outcome", "ref_id": outcome, "config": {"outcome": outcome}}]}


class TrailingCompileTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "debounce.db"))
        app_main.storage = self.storage
        self.tenant = self.storage.default_tenant_id
        patcher = mock.patch.object(app_main, "BUNDLE_COMPILE_DEBOUNCE_SECONDS", WINDOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _latest_outcome(self):
        bundle = self.storage.latest_bundle(tenant_id=self.tenant)
        policies = (bundle or {}).get("content", {}).get("policies") or []
        probe = [p for p in policies if p.get("id") == "debounce_probe"]
        return probe[0]["steps"][0]["ref_id"] if probe else None

    def test_a_change_inside_the_window_is_compiled_when_it_closes(self):
        self.storage.create_policy(_policy("approve"), tenant_id=self.tenant)
        app_main.maybe_compile_bundle(self.tenant)
        self.assertEqual(self._latest_outcome(), "approve", "the control: the first change compiles at once")

        self.storage.update_policy("debounce_probe", _policy("reject"), tenant_id=self.tenant)
        app_main.maybe_compile_bundle(self.tenant)  # inside the window
        self.assertEqual(self._latest_outcome(), "approve", "the debounce still applies immediately")

        deadline = time.time() + WINDOW + 3
        while time.time() < deadline and self._latest_outcome() != "reject":
            time.sleep(0.05)
        self.assertEqual(self._latest_outcome(), "reject", "the change inside the window was dropped")

    def test_a_burst_schedules_one_trailing_compile(self):
        self.storage.create_policy(_policy("approve"), tenant_id=self.tenant)
        app_main.maybe_compile_bundle(self.tenant)
        with mock.patch.object(app_main, "_compile_bundle_now", wraps=app_main._compile_bundle_now) as compiles:
            for _ in range(5):
                app_main.maybe_compile_bundle(self.tenant)
            time.sleep(WINDOW + 0.5)
        self.assertEqual(compiles.call_count, 1, "a burst of writes should coalesce into one compile")

    def test_a_failing_trailing_compile_is_logged_not_raised_into_the_thread(self):
        """Found in a full run: a timer firing after its store's database was removed raised out
        of the thread -- a bare stderr traceback, no log line."""
        import threading

        escaped = []
        previous = threading.excepthook
        threading.excepthook = lambda args: escaped.append(args.exc_value)
        self.addCleanup(setattr, threading, "excepthook", previous)

        class Broken:
            def mark_bundle_compile_queued(self, *_args, **_kwargs):
                raise RuntimeError("database is gone")

        with self.assertLogs("rulemind.bundle", level="WARNING") as logged:
            app_main._schedule_trailing_compile(Broken(), "tenant-x")
            time.sleep(WINDOW + 0.5)
        self.assertEqual(escaped, [], "the exception escaped the timer thread")
        self.assertIn("database is gone", logged.output[0])


if __name__ == "__main__":
    unittest.main()
