"""/decide must not write to the policy row (Tier 1.4).

`last_test_result` is a console artifact -- the "last tested" badge in the authoring UI. It was
written on every call to test_policy_entity, and /decide goes through that function, so every
production decision issued an UPDATE against the policy row.

Postgres takes a row lock for the duration of the transaction, so concurrent decisions on ONE
policy serialize behind each other. And a single hot policy is the normal shape of this
workload -- a lender has one live underwriting policy, not a thousand. Throughput on the
product's primary endpoint was capped at roughly one decision at a time by a UI badge.

SQLite in the test suite will not reproduce the contention, so these assert on the WRITE ITSELF:
the console path still records the badge, and the API path issues no policy write at all.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


class DecideDoesNotWriteThePolicyRowTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "hotpath.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.policy_id = self.client.get("/api/v1/policies", headers=self.headers).json()[0]["id"]

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

    def _policy_writes(self, call):
        """Count update_policy calls made during `call`, without disabling them."""
        real = app_main.storage.update_policy
        seen = []

        def spy(policy_id, payload, *a, **kw):
            seen.append((policy_id, sorted(payload)))
            return real(policy_id, payload, *a, **kw)

        with mock.patch.object(app_main.storage, "update_policy", side_effect=spy):
            call()
        return seen

    def test_decide_issues_no_policy_write(self):
        writes = self._policy_writes(lambda: self.client.post(
            "/api/v1/decide", headers=self.headers,
            json={"policyId": self.policy_id, "payload": {"age": 30}}))
        self.assertEqual(writes, [],
                         "/decide wrote to the policy row; concurrent decisions on one policy "
                         "will serialize on that row lock")

    def test_ten_decisions_still_issue_none(self):
        """The cost is per-decision, so the regression only shows at volume."""
        def burst():
            for _ in range(10):
                self.client.post("/api/v1/decide", headers=self.headers,
                                 json={"policyId": self.policy_id, "payload": {"age": 30}})
        self.assertEqual(self._policy_writes(burst), [])

    def test_the_console_still_records_the_badge(self):
        """The regression guard -- dropping the write entirely would break the authoring UI."""
        writes = self._policy_writes(lambda: self.client.post(
            "/api/v1/policies/{0}/execute".format(self.policy_id), headers=self.headers,
            json={"payload": {"age": 30}}))
        self.assertEqual([w[1] for w in writes], [["last_test_result"]],
                         "the console test no longer records last_test_result")

    def test_the_console_badge_is_readable_afterwards(self):
        self.client.post("/api/v1/policies/{0}/execute".format(self.policy_id),
                         headers=self.headers, json={"payload": {"age": 30}})
        policy = self.client.get("/api/v1/policies/{0}".format(self.policy_id),
                                 headers=self.headers).json()
        self.assertIsNotNone(policy.get("last_test_result"))
        self.assertIn(policy["last_test_result"]["outcome"], ("approve", "review", "reject"))

    def test_decide_does_not_stale_the_badge(self):
        """A production decision must not overwrite what the author last tested."""
        self.client.post("/api/v1/policies/{0}/execute".format(self.policy_id),
                         headers=self.headers, json={"payload": {"age": 30}})
        before = self.client.get("/api/v1/policies/{0}".format(self.policy_id),
                                 headers=self.headers).json()["last_test_result"]
        self.client.post("/api/v1/decide", headers=self.headers,
                         json={"policyId": self.policy_id, "payload": {"age": 99}})
        after = self.client.get("/api/v1/policies/{0}".format(self.policy_id),
                                headers=self.headers).json()["last_test_result"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
