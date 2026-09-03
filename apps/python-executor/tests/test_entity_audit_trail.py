"""Authoring changes reach the audit trail (Tier 1.5).

Rules, variables, scorecards and decision tables wrote an `EntityHistory` snapshot on every
create/update/delete but never an `AuditEvent`. Policies, identity, SDK, experiments, reviews
and webhooks all emitted one; these four were simply never wired up.

That is the wrong four to miss. They are the most-edited entity types in the product, and
"who changed which rule, when" is the question a governed decisioning platform exists to
answer. `EntityHistory` records *what* the entity became; only the audit trail records that a
change happened at all, and it is the audit trail that carries the tamper-evident hash chain.

Found by qa case 6.2, which had been written off as a test bug alongside two that genuinely
were. It was a product bug.
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

from app.storage import Storage  # noqa: E402


class EntityAuditTrailTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "audit.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def _events(self, entity_type=None):
        rows = self.storage.list_audit_events(tenant_id=self.tenant)
        return [e for e in rows if entity_type is None or e.get("entity_type") == entity_type]

    def _variable(self, vid="v_audit"):
        return {"id": vid, "name": "Audited", "source_id": "custom", "category": "custom",
                "code": "x = 1"}

    def _rule(self, rid="r_audit"):
        return {"id": rid, "name": "Audited Rule", "outcome": "approve",
                "nodes": [{"id": "n1", "type": "approve"}]}

    def test_creating_a_rule_is_audited(self):
        self.storage.create_rule(self._rule(), tenant_id=self.tenant)
        events = self._events("rule")
        self.assertEqual(len(events), 1, "a rule creation must appear in the audit trail")
        self.assertEqual(events[0]["event_type"], "rule_created")
        self.assertEqual(events[0]["entity_id"], "r_audit")

    def test_updating_and_deleting_a_rule_are_audited(self):
        self.storage.create_rule(self._rule(), tenant_id=self.tenant)
        self.storage.update_rule("r_audit", {"name": "Renamed"}, tenant_id=self.tenant)
        self.storage.delete_rule("r_audit", tenant_id=self.tenant)
        # list_audit_events orders newest-first, so reverse for a chronological read.
        kinds = [e["event_type"] for e in reversed(self._events("rule"))]
        self.assertEqual(kinds, ["rule_created", "rule_updated", "rule_deleted"])

    def test_a_deletion_is_recorded_even_though_the_row_is_gone(self):
        """The change most worth having in the trail. EntityHistory keeps the snapshot; only
        the audit event records that someone removed it."""
        self.storage.create_variable(self._variable(), tenant_id=self.tenant)
        self.storage.delete_variable("v_audit", tenant_id=self.tenant)
        self.assertIsNone(self.storage.get_variable("v_audit", tenant_id=self.tenant))
        self.assertIn("variable_deleted", [e["event_type"] for e in self._events("variable")])

    def test_all_four_authoring_entity_types_are_covered(self):
        """A regression guard for the shape of the original bug: three of four wired up is the
        same failure, just smaller."""
        self.storage.create_variable(self._variable("v_all"), tenant_id=self.tenant)
        self.storage.create_rule(self._rule("r_all"), tenant_id=self.tenant)
        self.storage.create_scorecard(
            {"id": "s_all", "name": "SC", "attributes": [],
             "bins": [{"variable_id": "v_all", "ranges": [{"min": 0, "max": 1, "points": 5}]}]},
            tenant_id=self.tenant)
        self.storage.create_decision_table(
            {"id": "dt_all", "name": "DT", "inputs": [], "outputs": [], "rows": []},
            tenant_id=self.tenant)

        covered = {e["entity_type"] for e in self._events()}
        for entity_type in ("variable", "rule", "scorecard", "decision_table"):
            self.assertIn(entity_type, covered, f"{entity_type} CRUD emits no audit event")

    def test_audit_events_join_the_hash_chain(self):
        """These events are not a side channel -- they must be tamper-evident like the rest."""
        self.storage.create_rule(self._rule("r_chain"), tenant_id=self.tenant)
        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertTrue(report["ok"], report["breaks"])
        self.assertGreaterEqual(report["verified"], 1)

    def test_a_failed_audit_write_does_not_roll_back_the_change(self):
        """Best-effort by contract: an authoring change that succeeded must not be undone
        because the audit write failed."""
        from unittest import mock

        with mock.patch.object(self.storage, "add_audit_event", side_effect=RuntimeError("boom")):
            created = self.storage.create_rule(self._rule("r_resilient"), tenant_id=self.tenant)
        self.assertEqual(created["id"], "r_resilient")
        self.assertIsNotNone(self.storage.get_rule("r_resilient", tenant_id=self.tenant))


if __name__ == "__main__":
    unittest.main()
