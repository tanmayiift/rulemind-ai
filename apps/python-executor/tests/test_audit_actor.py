"""Every audit event must name who did it.

`AuditEvent.user_id` is one of `audit_chain.CHAINED_FIELDS` — the tamper-evident hash has
always covered it — and **no caller ever set it**. So every `user_id` in the product was NULL
and the chain was protecting the absence of an answer. The read endpoint's docstring claims the
trail answers *"who changed which rule/variable/scorecard/policy, when"*
(`routers/insights.py:73`). It answered *what* and *when*.

`_audit_entity_change` (`storage.py:2655`) is the most-edited path — rules, variables,
scorecards, decision tables — and it never passed an actor. Neither did the other ~13 emitters.
Only the promotion ledger got this right, via `Promotion.promoted_by` (`main.py:1050`).

The fix defaults the actor inside `add_audit_event` rather than at the call sites, because the
next emitter added would have missed it exactly the way these did. The middleware already
resolves the identity — an API key id, or `member:<id>` for a session — and pushes it into a
contextvar (`middleware.py:543, :552, :601`), which is precisely what `active_actor()` returns.

These tests pin: the actor is captured, an explicit one still wins, no context yields NULL
rather than a fabricated actor, and **the hash chain still verifies** — because adding a field
the chain already covers is exactly the kind of change that could silently break every
pre-existing row.
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

from app import context  # noqa: E402
from app.audit_chain import CHAINED_FIELDS  # noqa: E402
from app.storage import Storage  # noqa: E402


class AuditActorTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "audit.db"))
        self.tenant = self.storage.default_tenant_id

    def _events(self):
        return self.storage.list_audit_events(tenant_id=self.tenant)

    def test_user_id_is_a_chained_field(self):
        """If this ever stops being true the fix below is cosmetic rather than attestable."""
        self.assertIn("user_id", CHAINED_FIELDS)

    def test_an_authoring_change_records_the_actor(self):
        token = context.set_current_api_key_id("key_abc123")
        try:
            self.storage.create_rule({"id": "audit_actor_probe", "name": "probe", "status": "dev", "nodes": []},
                                     tenant_id=self.tenant)
        finally:
            context.reset_current_api_key_id(token)
        rule_events = [e for e in self._events() if e.get("entity_id") == "audit_actor_probe"]
        self.assertTrue(rule_events, "creating a rule must emit an audit event")
        self.assertEqual(rule_events[0].get("user_id"), "key_abc123")

    def test_a_member_session_is_recorded_as_the_member(self):
        """middleware.py:552 sets `member:<id>` for a session, and that is the identity that
        matters for dual control and for the question a regulator asks."""
        token = context.set_current_api_key_id("member:u-42")
        try:
            self.storage.add_audit_event({"event_type": "rule_update", "entity_type": "rule",
                                          "entity_id": "r2", "detail": "x"}, tenant_id=self.tenant)
        finally:
            context.reset_current_api_key_id(token)
        self.assertEqual(self._events()[0].get("user_id"), "member:u-42")

    def test_an_explicit_actor_still_wins(self):
        token = context.set_current_api_key_id("key_from_context")
        try:
            self.storage.add_audit_event({"event_type": "x", "detail": "d", "user_id": "explicit"},
                                         tenant_id=self.tenant)
        finally:
            context.reset_current_api_key_id(token)
        self.assertEqual(self._events()[0].get("user_id"), "explicit")

    def test_no_request_context_means_NULL_not_a_guess(self):
        """A scheduler job has no actor. Recording one would be a fabrication, which is the
        failure this whole area is being fixed for."""
        self.storage.add_audit_event({"event_type": "slo_breach", "detail": "d"},
                                     tenant_id=self.tenant)
        self.assertIsNone(self._events()[0].get("user_id"))

    def test_the_hash_chain_still_verifies_with_an_actor_present(self):
        """The chain covers user_id, so writing it changes every new row's digest. Pre-existing
        rows keep theirs (they hash user_id: null); the chain must not fork."""
        token = context.set_current_api_key_id("key_chain")
        try:
            for i in range(5):
                self.storage.add_audit_event(
                    {"event_type": "rule_update", "entity_type": "rule", "entity_id": "r%d" % i,
                     "detail": "d"}, tenant_id=self.tenant)
        finally:
            context.reset_current_api_key_id(token)
        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertEqual(report.get("breaks"), [], report)
        self.assertGreaterEqual(report.get("verified", 0), 5)

    def test_a_chain_mixing_actored_and_unactored_rows_verifies(self):
        """The realistic upgrade path: rows written before this fix, then rows after."""
        self.storage.add_audit_event({"event_type": "before", "detail": "d"}, tenant_id=self.tenant)
        token = context.set_current_api_key_id("key_after")
        try:
            self.storage.add_audit_event({"event_type": "after", "detail": "d"}, tenant_id=self.tenant)
        finally:
            context.reset_current_api_key_id(token)
        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertEqual(report.get("breaks"), [], report)


if __name__ == "__main__":
    unittest.main()
