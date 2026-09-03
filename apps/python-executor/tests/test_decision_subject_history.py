"""Every decision for a customer is persisted and retrievable (Tier 1.1).

The worst bug this repo has had. `/decide` wrote the request's `userId` into `Decision.client_id`
so that case-wise history had a column to filter on -- but `client_id` sits under
`UNIQUE(tenant_id, client_id)`, the on-device SDK dedupe key that exists so an at-least-once
retry never double-counts.

A userId is a SUBJECT identifier. It repeats on every re-decision of the same customer, which is
the normal case: a renewal, a re-application, a re-KYC. So the second decision for any customer
violated the constraint. The decision-log write is async, so the caller had already received
**200 and a decision_id that would never exist**.

Everything downstream followed from that one column:
  * no audit row -- the compliance artifact the product exists to produce
  * `record_usage` sat after the flush, so the tenant was never billed for the repeat decision
  * `/decisions/{id}/reason-codes` 404s, so no adverse-action notice
  * the WAL never marked committed, replaying the doomed row at every boot

These tests fail against the old schema for four different reasons, which is the point: one
column conflated two concepts and the blast radius was everywhere.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


class DecisionSubjectHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "subject.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def _decide(self, user_id, **extra):
        payload = {"policy_id": "p1", "outcome": "approve", "payload": {"x": 1},
                   "user_id": user_id, "source": "api"}
        payload.update(extra)
        return self.storage.add_decision(payload, tenant_id=self.tenant)

    def test_repeat_decisions_for_one_customer_all_persist(self):
        """The headline. Three decisions for one customer used to yield one row."""
        ids = [self._decide("cust-1")["id"] for _ in range(3)]
        self.assertEqual(len(set(ids)), 3, "each decision needs its own server-generated id")

        history = self.storage.list_decisions(tenant_id=self.tenant, user_id="cust-1")
        self.assertEqual(len(history), 3)
        self.assertEqual({d["id"] for d in history}, set(ids))

    def test_every_repeat_decision_is_billed(self):
        """record_usage ran after the flush, so a dropped write silently under-billed."""
        for _ in range(3):
            self._decide("cust-2")
        self.assertEqual(self.storage.get_usage_total("decisions", tenant_id=self.tenant), 3)

    def test_the_subject_is_not_the_idempotency_key(self):
        """client_id must stay NULL for a server decision -- the model's own documented
        contract, and the one the /decide path was breaking."""
        row = self._decide("cust-3")
        self.assertEqual(row["user_id"], "cust-3")
        self.assertIsNone(row["client_id"],
                          "a server decision must not occupy the device dedupe key")

    def test_device_dedupe_still_works(self):
        """The constraint is doing a real job for its actual purpose -- an SDK replaying the
        same client_id must NOT create a second row. Fixing the subject must not weaken this."""
        records = [{"id": "dev-abc", "policy_id": "p1", "outcome": "approve", "payload": {}}]
        first = self.storage.add_decisions_batch(records, tenant_id=self.tenant)
        replay = self.storage.add_decisions_batch(records, tenant_id=self.tenant)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(replay["inserted"], 0)
        self.assertEqual(replay["duplicates"], 1)

    def test_two_customers_do_not_collide(self):
        self._decide("cust-a")
        self._decide("cust-b")
        self.assertEqual(len(self.storage.list_decisions(tenant_id=self.tenant, user_id="cust-a")), 1)
        self.assertEqual(len(self.storage.list_decisions(tenant_id=self.tenant, user_id="cust-b")), 1)

    def test_subject_history_is_tenant_scoped(self):
        """Two tenants can legitimately use the same customer id."""
        other = self.storage.create_tenant("Other Co")["id"]
        self._decide("shared-id")
        self.storage.add_decision({"policy_id": "p1", "outcome": "reject", "payload": {},
                                   "user_id": "shared-id", "source": "api"}, tenant_id=other)
        mine = self.storage.list_decisions(tenant_id=self.tenant, user_id="shared-id")
        theirs = self.storage.list_decisions(tenant_id=other, user_id="shared-id")
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(theirs), 1)
        self.assertNotEqual(mine[0]["id"], theirs[0]["id"])


class DecideEndpointSubjectTest(unittest.TestCase):
    """Drives the real /decide path.

    The storage-level tests above pass `user_id=` directly, so they do NOT cover the line where
    the bug actually lived -- `executor.py`'s record construction. Verified by mutation:
    restoring `"client_id": ctx.user_id or payload_client_id` leaves every storage-level test
    green. This class is what fails.
    """

    def setUp(self):
        os.environ["RULEMIND_SEED_DEMO"] = "1"
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "decide.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def test_three_decides_for_one_user_all_persist(self):
        policies = self.client.get("/api/v1/policies", headers=self.headers).json()
        self.assertTrue(policies, "seeded policies are the fixture here")
        policy_id = policies[0]["id"]

        returned = []
        for i in range(3):
            res = self.client.post("/api/v1/decide", headers=self.headers,
                                   json={"policyId": policy_id, "payload": {"age": 30 + i},
                                         "userId": "repeat-customer"})
            self.assertEqual(res.status_code, 200, res.text[:300])
            returned.append(res.json().get("decision_id"))

        from app import decision_log
        decision_log.flush()  # the write is async; that is why the drop was invisible

        history = app_main.storage.list_decisions(tenant_id=self.tenant, user_id="repeat-customer")
        self.assertEqual(len(history), 3,
                         "a re-decided customer must have every decision on record")

        # The decision_id /decide hands back must actually resolve -- the old path returned an
        # id for a row that was never written.
        stored = {d["id"] for d in history}
        for decision_id in returned:
            self.assertIn(decision_id, stored,
                          "/decide returned a decision_id that does not exist")

    def test_decide_leaves_the_device_dedupe_key_free(self):
        policy_id = self.client.get("/api/v1/policies", headers=self.headers).json()[0]["id"]
        self.client.post("/api/v1/decide", headers=self.headers,
                         json={"policyId": policy_id, "payload": {"age": 30}, "userId": "u-1"})
        from app import decision_log
        decision_log.flush()
        row = app_main.storage.list_decisions(tenant_id=self.tenant, user_id="u-1")[0]
        self.assertIsNone(row["client_id"],
                          "the server path must not occupy the on-device idempotency key")


if __name__ == "__main__":
    unittest.main()
