"""A paused execution that nothing will resume must be findable.

A multi-step policy that pauses -- on a `review_gate`, or because an SDK client resumes it at
the edge -- writes a durable `workflow_executions` row. The only ways to reach one were
`GET /sdk/v1/executions/{id}` and its resume twin, and both need the id you are trying to find.
There was no list, no count, no screen, and no sweeper. So an execution that paused and was
never resumed was not merely un-resumed: it was UNDISCOVERABLE. "Nothing ever resumes a paused
execution" was not an observable claim about this system.

Two things matter in the design and both are tested here.

`stuck` EXCLUDES anything with an open review task. Those are not stuck, they are waiting, and
`check_review_timeouts` already owns them; reporting them here would make the signal noise, and
a sweeper nobody reads is the same as no sweeper.

The sweep REPORTS, it does not expire. A paused execution holds a real decision in flight -- a
customer's application, mid-underwrite -- so a sweeper that guessed wrong about "orphaned"
would abandon a live case. `EXECUTION_SWEEP_EXPIRE=1` opts in, and the default-off is the same
call `DECISION_ARCHIVE_SINK=none` makes: a destructive default silently changes what an upgrade
does to data already in the table.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.scheduler import sweep_stuck_executions  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

LONG_AGO = datetime.utcnow() - timedelta(days=3)
RECENT = datetime.utcnow() - timedelta(minutes=5)


class StuckExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "stuck.db"))
        app_main.storage = self.storage
        self.tenant = self.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": self.storage.default_api_key or ""}

    def _execution(self, execution_id, status="paused", paused_at=LONG_AGO, policy_id="p1"):
        return self.storage.create_workflow_execution({
            "id": execution_id, "policy_id": policy_id, "status": status,
            "context": {"payload": {"pan": "ABCDE1234F"}}, "current_step_index": 2,
            "trigger_type": "api", "started_at": paused_at, "paused_at": paused_at,
        }, tenant_id=self.tenant)

    # ── enumeration ──────────────────────────────────────────────────────────

    def test_executions_can_be_listed_at_all(self):
        """The baseline that did not exist. Without a list endpoint an operator cannot discover
        that a stuck execution exists, let alone act on it."""
        self._execution("ex-1")
        rows = self.client.get("/api/v1/executions", headers=self.headers).json()
        self.assertEqual([r["id"] for r in rows], ["ex-1"])

    def test_the_list_omits_the_execution_context(self):
        """The context carries the full decision payload and every computed variable. 200 of
        them in a list view is megabytes of PII for a screen that only needs ids and statuses."""
        self._execution("ex-pii")
        row = self.client.get("/api/v1/executions", headers=self.headers).json()[0]
        self.assertNotIn("context", row)
        self.assertIn("ABCDE1234F",
                      str(self.storage.get_workflow_execution("ex-pii", tenant_id=self.tenant)),
                      "the context must still be fetchable for the ONE you picked")

    def test_the_list_filters_by_status_and_policy(self):
        self._execution("ex-paused")
        self._execution("ex-done", status="completed")
        self._execution("ex-other", policy_id="p2")
        paused = self.client.get("/api/v1/executions?status=paused", headers=self.headers).json()
        self.assertEqual({r["id"] for r in paused}, {"ex-paused", "ex-other"})
        p2 = self.client.get("/api/v1/executions?policy_id=p2", headers=self.headers).json()
        self.assertEqual({r["id"] for r in p2}, {"ex-other"})

    def test_the_list_is_tenant_scoped(self):
        self._execution("ex-mine")
        other = self.storage.create_tenant("Other Co")["id"]
        key = self.storage.generate_api_key_for_tenant(other)["plaintext"]
        rows = self.client.get("/api/v1/executions", headers={"x-api-key": key}).json()
        self.assertEqual(rows, [])

    # ── stuck vs waiting ─────────────────────────────────────────────────────

    def test_an_execution_with_an_open_review_task_is_waiting_not_stuck(self):
        """THE discrimination. A paused execution with someone still expected to act on it is
        not stuck, and check_review_timeouts already owns it. Counting it here would drown the
        real signal."""
        self._execution("ex-waiting")
        self.storage.create_review_task({
            "id": "task-1", "execution_id": "ex-waiting", "policy_id": "p1",
            "queue": "default", "status": "pending", "payload": {},
        }, tenant_id=self.tenant)
        body = self.client.get("/api/v1/executions/stuck", headers=self.headers).json()
        self.assertEqual(body["paused"], 1)
        self.assertEqual(body["awaiting_review"], 1)
        self.assertEqual(body["stuck"], [])

    def test_an_execution_whose_review_task_was_completed_IS_stuck(self):
        """The real-world shape: the queue was drained, the task is closed, and the execution
        it was gating is still paused with nothing left to resume it."""
        self._execution("ex-orphan")
        self.storage.create_review_task({
            "id": "task-2", "execution_id": "ex-orphan", "policy_id": "p1",
            "queue": "default", "status": "completed", "payload": {},
        }, tenant_id=self.tenant)
        body = self.client.get("/api/v1/executions/stuck", headers=self.headers).json()
        self.assertEqual([item["id"] for item in body["stuck"]], ["ex-orphan"])

    def test_a_recently_paused_execution_is_not_reported(self):
        """The threshold has to bite, or every in-flight review gate is an incident."""
        self._execution("ex-fresh", paused_at=RECENT)
        body = self.client.get("/api/v1/executions/stuck?older_than_minutes=1440",
                               headers=self.headers).json()
        self.assertEqual(body["stuck"], [])

    # ── the sweep ────────────────────────────────────────────────────────────

    def test_the_sweep_files_an_error_event_naming_the_count_and_the_oldest(self):
        """Reporting into /api/v1/audit/errors is the whole point: an operator does not read
        scheduler logs, and a finding nobody sees is the same as no finding."""
        self._execution("ex-a")
        self._execution("ex-b")
        summary = sweep_stuck_executions(self.storage)
        self.assertEqual(summary["tenants"][self.tenant]["stuck"], 2)
        events = self.storage.list_error_events(tenant_id=self.tenant)
        sweep_events = [e for e in events if e.get("scope") == "executions"]
        self.assertEqual(len(sweep_events), 1)
        self.assertIn("2 execution(s)", sweep_events[0]["message"])
        self.assertIn("Nothing will resume them", sweep_events[0]["message"])

    def test_the_SWEEP_also_excludes_executions_awaiting_review(self):
        """Added after a mutation survived. The sweep had its own copy of the "awaiting review"
        filter; breaking that copy passed every test, because the only test of the rule went
        through the endpoint and exercised the OTHER copy. There is one implementation now
        (`find_stuck_executions`), and this pins it from the sweep's side too."""
        self._execution("ex-waiting")
        self.storage.create_review_task({
            "id": "task-w", "execution_id": "ex-waiting", "policy_id": "p1",
            "queue": "default", "status": "pending", "payload": {},
        }, tenant_id=self.tenant)
        summary = sweep_stuck_executions(self.storage)
        self.assertEqual(summary["tenants"], {},
                         "the sweep reported an execution that a human is still expected to act on")

    def test_there_is_one_stuck_filter_not_two(self):
        """Source-level, because two copies that happen to agree today pass every behavioural
        test until the day one is edited."""
        ops = (APP_ROOT / "app" / "routers" / "operations.py").read_text(encoding="utf-8")
        self.assertIn("find_stuck_executions", ops)
        self.assertNotIn('("pending", "escalated")', ops,
                         "operations.py has its own copy of the awaiting-review filter again")

    def test_the_sweep_does_not_expire_by_default(self):
        """A paused execution holds a customer's application mid-underwrite. Default-destructive
        would mean an upgrade silently abandons live cases."""
        self._execution("ex-keep")
        sweep_stuck_executions(self.storage)
        row = self.storage.get_workflow_execution("ex-keep", tenant_id=self.tenant)
        self.assertEqual(row["status"], "paused")

    def test_expiry_is_opt_in_and_works(self):
        self._execution("ex-expire")
        with mock.patch.dict(os.environ, {"EXECUTION_SWEEP_EXPIRE": "1"}):
            sweep_stuck_executions(self.storage)
        row = self.storage.get_workflow_execution("ex-expire", tenant_id=self.tenant)
        self.assertEqual(row["status"], "expired")

    def test_the_sweep_reports_nothing_when_there_is_nothing(self):
        """A sweep that files an event on a healthy install trains people to ignore it."""
        self._execution("ex-fresh", paused_at=RECENT)
        summary = sweep_stuck_executions(self.storage)
        self.assertEqual(summary["tenants"], {})
        self.assertEqual([e for e in self.storage.list_error_events(tenant_id=self.tenant)
                          if e.get("scope") == "executions"], [])

    def test_one_tenants_failure_does_not_stop_the_sweep(self):
        """It iterates every tenant; a single bad row must not leave the rest unswept."""
        self._execution("ex-ok")
        real = self.storage.list_review_tasks
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(*args, **kwargs)

        with mock.patch.object(self.storage, "list_review_tasks", side_effect=flaky):
            summary = sweep_stuck_executions(self.storage)   # must not raise
        self.assertIsInstance(summary, dict)

    def test_the_job_is_leader_gated(self):
        """N replicas each filing the same error event turns the signal into noise. Every other
        sweep in this scheduler is gated the same way."""
        source = (APP_ROOT / "app" / "scheduler.py").read_text(encoding="utf-8")
        self.assertIn("async def _scheduled_sweep_stuck_executions", source)
        block = source.split("async def _scheduled_sweep_stuck_executions", 1)[1][:200]
        self.assertIn("if not _IS_LEADER", block)


if __name__ == "__main__":
    unittest.main()
