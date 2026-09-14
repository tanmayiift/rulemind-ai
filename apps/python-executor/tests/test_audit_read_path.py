"""The audit trail must be readable over HTTP (completes Tier 1.5).

Wiring AuditEvent writes into entity CRUD only fixed half the problem. Events were being
recorded, but every reader in the codebase filtered to one specific event_type (slo_breach,
cron_executed) or one entity_id -- there was no route that listed the trail at all. For a
governed decisioning product whose audit trail IS the compliance artifact, "who changed this
rule" was answerable only by opening the database.

qa case 6.2 had been reporting this the whole time. It probes GET /api/v1/audit, gets a 404,
silently falls back to /api/v1/audit/decisions -- which lists decisions, not authoring changes --
and reports "new rule not found in audit trail". It reads like a broken test; it was not.
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402

RULE_TREE = {"type": "group", "id": "g", "logic": "AND",
             "children": [{"type": "condition", "id": "c1", "variable": "age",
                           "operator": ">=", "value": 18}],
             "onPass": "pass", "onFail": "reject"}


class AuditReadPathTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "audit.db"))
        self.tenant = app_main.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

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

    def _make_rule(self, rule_id):
        return self.client.post("/api/v1/rules", headers=self.headers,
                                json={"id": rule_id, "name": rule_id, "tree": RULE_TREE})

    def test_the_endpoint_exists(self):
        res = self.client.get("/api/v1/audit", headers=self.headers)
        self.assertEqual(res.status_code, 200,
                         "GET /api/v1/audit 404s -- the audit trail is write-only over HTTP")

    def test_a_rule_creation_is_visible_in_the_trail(self):
        """qa 6.2, end to end."""
        self._make_rule("audit_probe_1")
        events = self.client.get("/api/v1/audit", headers=self.headers).json()
        matching = [e for e in events if e.get("entity_id") == "audit_probe_1"]
        self.assertTrue(matching, "the new rule does not appear in the audit trail")
        self.assertEqual(matching[0]["event_type"], "rule_created")
        self.assertIsNotNone(matching[0].get("created_at"))

    def test_the_full_lifecycle_is_traceable(self):
        """who/what/when for a create, an edit and a delete -- the actual governance question."""
        self._make_rule("audit_probe_2")
        self.client.put("/api/v1/rules/audit_probe_2", headers=self.headers,
                        json={"name": "renamed", "tree": RULE_TREE})
        self.client.delete("/api/v1/rules/audit_probe_2", headers=self.headers)
        events = self.client.get("/api/v1/audit", headers=self.headers,
                                 params={"entity_id": "audit_probe_2"}).json()
        self.assertEqual({e["event_type"] for e in events},
                         {"rule_created", "rule_updated", "rule_deleted"})

    def test_it_filters_by_entity(self):
        self._make_rule("audit_probe_3")
        self._make_rule("audit_probe_4")
        events = self.client.get("/api/v1/audit", headers=self.headers,
                                 params={"entity_id": "audit_probe_3"}).json()
        self.assertTrue(events)
        self.assertEqual({e["entity_id"] for e in events}, {"audit_probe_3"})

        by_type = self.client.get("/api/v1/audit", headers=self.headers,
                                  params={"entity_type": "rule"}).json()
        self.assertTrue(by_type)
        self.assertEqual({e["entity_type"] for e in by_type}, {"rule"})

    def test_it_is_paginated(self):
        """The table is append-only and unbounded; an unpaged read of a mature tenant is the
        same timeout-and-OOM that list_decisions already guards against."""
        for i in range(5):
            self._make_rule("audit_page_{0}".format(i))
        page = self.client.get("/api/v1/audit", headers=self.headers,
                               params={"limit": 2}).json()
        self.assertEqual(len(page), 2)
        second = self.client.get("/api/v1/audit", headers=self.headers,
                                 params={"limit": 2, "offset": 2}).json()
        self.assertEqual(len(second), 2)
        self.assertFalse({e["id"] for e in page} & {e["id"] for e in second},
                         "pages overlap -- a reader would see the same event twice and miss one")

    def test_paging_is_stable_for_events_written_in_the_same_tick(self):
        """created_at is not unique, so ordering by it alone lets same-tick events swap between
        pages -- a paging reader silently skips one. Ordering by seq (unique per tenant) fixes
        it, and same-tick writes are the normal case for a burst of authoring changes."""
        # Force the collision instead of hoping for it: created_at has microsecond resolution
        # here, so ordinary writes never actually tie and the bug stays invisible. In production
        # a burst of authoring changes ties routinely.
        from datetime import datetime, timedelta

        from app.models import AuditEvent

        for i in range(6):
            self._make_rule("audit_tick_{0}".format(i))
        # Stamp created_at in the REVERSE of chain order. Ties alone are not enough to expose
        # the bug -- SQLite happens to break them by rowid, which matches seq, so the two
        # orderings agree by luck. Reversing them makes the query commit to one, and the chain
        # order (seq) is the only correct answer: it is what the hash chain is verified in, so
        # a trail paged in created_at order would not line up with its own verification.
        with app_main.storage.connect() as session:
            rows = sorted(session.query(AuditEvent).all(), key=lambda r: r.seq or 0)
            for offset, row in enumerate(rows):
                row.created_at = datetime(2026, 1, 1, 12, 0, 0) - timedelta(seconds=offset)

        everything = self.client.get("/api/v1/audit", headers=self.headers,
                                     params={"limit": 100}).json()
        paged = []
        for offset in range(0, len(everything), 2):
            paged.extend(self.client.get("/api/v1/audit", headers=self.headers,
                                         params={"limit": 2, "offset": offset}).json())
        self.assertEqual([e["id"] for e in everything], [e["id"] for e in paged],
                         "paging through the trail did not reproduce the unpaged order")
        seqs = [e["seq"] for e in everything if e.get("seq") is not None]
        self.assertEqual(seqs, sorted(seqs, reverse=True),
                         "the trail is not ordered by chain position, so paging it does not "
                         "line up with the order verify_audit_chain walks")

    def test_it_does_not_serve_another_tenants_history(self):
        """An audit read that falls back to the default tenant hands one customer's change
        history to another."""
        self._make_rule("first_tenant_rule")
        other = app_main.storage.create_tenant("Other Co")
        issued = app_main.storage.generate_api_key_for_tenant(other["id"])
        other_key = issued["plaintext"]
        self.assertTrue(other_key, "could not issue a key for the second tenant")
        app_main.storage.create_rule(
            {"id": "other_tenant_rule", "name": "Theirs", "tree": RULE_TREE, "version": 1},
            tenant_id=other["id"])

        # Read AS the second tenant. The storage layer resolves a missing tenant_id to the FIRST
        # tenant, so a route that forgets to pass one still looks correct from the default
        # tenant's seat -- it reads its own data either way. The fail-open only shows from a
        # second tenant, which is the seat every real customer but the first is sitting in.
        events = self.client.get("/api/v1/audit", headers={"x-api-key": other_key},
                                 params={"limit": 500}).json()
        ids = {e.get("entity_id") for e in events}
        self.assertIn("other_tenant_rule", ids, "the tenant cannot see its own audit trail")
        self.assertNotIn("first_tenant_rule", ids,
                         "one tenant's audit read returned another tenant's change history")


if __name__ == "__main__":
    unittest.main()
