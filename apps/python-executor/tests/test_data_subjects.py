"""Data-subject access and erasure (plan 6.7).

Nothing existed for either. Every assertion that matters here is checked from the OUTSIDE --
the subject's own values must be absent from what the API and the tables return afterwards, and
what a lender must keep must still be there -- because an erasure that updates the rows it knows
about and misses one is indistinguishable from a working one unless something looks.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app.main as app_main  # noqa: E402
from app import data_subjects  # noqa: E402
from app.models import AuditEvent, IdempotencyRecord, WebhookDelivery, WorkflowExecution  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

SUBJECT = "applicant-7731"
PAN = "ABCDE1234A"          # the applicant's own data, which must not survive
SCORE = 612                 # the applicant's own number, also inside the reason-code sentence
THRESHOLD = 650


def _decision(decision_id: str, user_id: str = SUBJECT, outcome: str = "reject") -> dict:
    return {
        "id": decision_id, "user_id": user_id, "policy_id": "pl_policy", "outcome": outcome,
        "policy_version": 4, "payload": {"custom": {"pan": PAN, "name": "Asha Rao"}},
        "computed_variables": {"bureau_score": SCORE},
        "rule_results": [{"rule_id": "gate", "conditions": [{
            "variable_id": "bureau_score", "variable_name": "Bureau score", "operator": ">=",
            "threshold": THRESHOLD, "value": SCORE, "passed": False}]}],
        "scorecard_result": {"total": SCORE, "points": {"bureau": 40}},
        "trace": [{"type": "rule", "inputs": {"bureau_score": SCORE, "pan": PAN}}],
    }


class DataSubjectTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "subjects.db"))
        app_main.storage = self.storage
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.tenant = self.storage.default_tenant_id
        self.admin = {"x-api-key": self.storage.generate_api_key_for_tenant(self.tenant, role="admin")["plaintext"]}
        self.storage.add_decision(_decision("d-1"), tenant_id=self.tenant)
        self.storage.add_decision(_decision("d-2", outcome="approve"), tenant_id=self.tenant)
        self.storage.add_decision(_decision("d-other", user_id="someone-else"), tenant_id=self.tenant)
        # The subject in the other stores erasure must reach.
        with self.storage.connect() as session:
            session.add(WorkflowExecution(id="ex-1", tenant_id=self.tenant, policy_id="pl_policy", status="paused",
                                          context={"user_id": SUBJECT, "payload": {"custom": {"pan": PAN}}},
                                          current_step_index=1))
            session.add(WebhookDelivery(tenant_id=self.tenant, subscription_id="sub-1", event_type="decision.completed",
                                        event={"type": "decision.completed", "data": {"decision_id": "d-1", "user_id": SUBJECT}}))
            session.add(IdempotencyRecord(tenant_id=self.tenant, key="idem-1", request_hash="h",
                                          status_code=200, response_body=json.dumps({"decision_id": "d-1", "pan": PAN})))

    def _erase(self):
        return self.client.post(f"/api/v1/data-subjects/{SUBJECT}/erase", headers=self.admin,
                                json={"reason": "Art. 17 request #4471 verified"})

    def test_an_access_request_returns_the_subjects_decisions_and_no_one_elses(self):
        response = self.client.get(f"/api/v1/data-subjects/{SUBJECT}", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(sorted(d["id"] for d in body["decisions"]), ["d-1", "d-2"])
        self.assertEqual(body["decisions"][0]["payload"]["custom"]["pan"], PAN)
        self.assertEqual([e["id"] for e in body["executions"]], ["ex-1"])
        self.assertTrue(body["not_covered"], "an export must say what it cannot see")

    def test_erasure_removes_the_subjects_data_from_every_store_it_reaches(self):
        receipt = self._erase()
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["erased"]["decisions"], 2)
        # Nothing the API returns about those decisions carries the applicant's data...
        for decision_id in ("d-1", "d-2"):
            text = json.dumps(self.storage.get_decision(decision_id, tenant_id=self.tenant))
            self.assertNotIn(PAN, text)
            self.assertNotIn("Asha", text)
            self.assertNotIn(str(SCORE), text.replace("erased:", ""))
            self.assertNotIn(SUBJECT, text)
        # ...and nor does any other table it lives in.
        with self.storage.connect() as session:
            self.assertNotIn(PAN, json.dumps(session.get(WorkflowExecution, "ex-1").context))
            delivery = session.scalars(select(WebhookDelivery)).one()
            self.assertNotIn(SUBJECT, json.dumps(delivery.event))
            self.assertEqual(session.scalars(select(IdempotencyRecord)).all(), [], "the cached response survived")

    def test_what_a_lender_must_keep_is_kept(self):
        self._erase()
        kept = self.storage.get_decision("d-1", tenant_id=self.tenant)
        self.assertEqual((kept["outcome"], kept["policy_id"], kept["policy_version"]), ("reject", "pl_policy", 4))
        self.assertIsNotNone(kept["erased_at"])
        codes = self.client.get("/api/v1/decisions/d-1/reason-codes", headers=self.admin).json()["reason_codes"]
        self.assertEqual(len(codes), 1, "the decline notice's reason codes went with the data")
        self.assertEqual(codes[0]["threshold"], THRESHOLD)
        self.assertNotIn(str(SCORE), json.dumps(codes), "the applicant's own number survived in the reason")

    def test_other_subjects_are_untouched(self):
        self._erase()
        other = self.storage.get_decision("d-other", tenant_id=self.tenant)
        self.assertEqual(other["payload"]["custom"]["pan"], PAN)
        self.assertIsNone(other["erased_at"])

    def test_erasing_twice_is_idempotent_and_the_audit_names_no_one(self):
        first = self._erase().json()
        second = self._erase().json()
        self.assertEqual(second["erased"]["decisions"], 0, "the second erasure found the subject again")
        self.assertTrue(first["subject"].startswith("erased:"))
        with self.storage.connect() as session:
            events = session.scalars(select(AuditEvent).where(AuditEvent.event_type == "data_subject_erased")).all()
            self.assertEqual(len(events), 2)
            self.assertNotIn(SUBJECT, json.dumps([e.entity_id for e in events] + [e.detail for e in events]))

    def test_the_pseudonym_is_keyed_not_a_plain_hash(self):
        import hashlib

        alias = data_subjects.pseudonym(SUBJECT, self.tenant)
        self.assertNotIn(hashlib.sha256(SUBJECT.encode()).hexdigest()[:24], alias)
        self.assertEqual(alias, data_subjects.pseudonym(SUBJECT, self.tenant))
        self.assertNotEqual(alias, data_subjects.pseudonym(SUBJECT, "another-tenant"))

    def test_only_an_admin_can_export_or_erase(self):
        viewer = {"x-api-key": self.storage.generate_api_key_for_tenant(self.tenant, role="policy_maker")["plaintext"]}
        self.assertEqual(self.client.get(f"/api/v1/data-subjects/{SUBJECT}", headers=viewer).status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/data-subjects/{SUBJECT}/erase", headers=viewer,
                                          json={"reason": "Art. 17 request #4471"}).status_code, 403)

    def test_another_tenant_cannot_see_or_erase_this_subject(self):
        other = self.storage.create_tenant("Other")["id"]
        key = {"x-api-key": self.storage.generate_api_key_for_tenant(other, role="admin")["plaintext"]}
        self.assertEqual(self.client.get(f"/api/v1/data-subjects/{SUBJECT}", headers=key).json()["counts"]["decisions"], 0)
        self.client.post(f"/api/v1/data-subjects/{SUBJECT}/erase", headers=key, json={"reason": "cross-tenant attempt"})
        self.assertEqual(self.storage.get_decision("d-1", tenant_id=self.tenant)["payload"]["custom"]["pan"], PAN)

    def test_a_reason_is_required(self):
        response = self.client.post(f"/api/v1/data-subjects/{SUBJECT}/erase", headers=self.admin, json={})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
