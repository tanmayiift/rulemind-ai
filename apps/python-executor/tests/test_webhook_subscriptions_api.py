"""Outbound webhook subscriptions, at the HTTP surface: the definition of done (parity P03).

`docs/parity/LENS-QA.md` requires all five, per entity, and each is a named test below:
tenant isolation (a negative), RBAC across all five roles, an audit event carrying the ACTOR,
an OpenAPI snapshot that regenerates clean (tests/test_openapi_gate.py, refreshed with the seven
additive operations), and at least one negative test rather than only the happy path.

THE RBAC DECISION IS THE INTERESTING ONE, so it gets the most tests. These routes require
`manage_access`, not `author`, which puts a SUBSCRIPTION in a different class from the inbound
`/api/v1/webhooks` entity next to it. The reasoning: a `policy_maker` can already POST to an
arbitrary URL from an action step, so this is not about the capability to reach the network. It
is that a subscription is standing egress configuration -- it sends this workspace's decision
data to a third party, for every matching event, until someone removes it -- which belongs with
`/api/v1/settings` and `/api/v1/branding`, not with the authoring entities.
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
# Not optional: middleware.py early-returns on AUTH_MODE=none, which skips the API-key check and
# RBAC entirely. An authorization suite running without auth proves nothing.
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import webhook_dispatch  # noqa: E402
from app.storage import Storage  # noqa: E402

BASE = "/api/v1/webhook-subscriptions"


def body(**overrides):
    payload = {"name": "ops alerts", "url": "https://hooks.example.com/rulemind",
               "event_types": ["decision.rejected"]}
    payload.update(overrides)
    return payload


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "subs.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.owner_key = app_main.storage.default_api_key or ""
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        self.client.close()
        # Dispose the engine, not just the tempdir. db._engine_cache is keyed by URL and never
        # evicts -- correct in a server (a handful of URLs for the process lifetime), a slow leak
        # in a suite where every test mints a fresh temp path. The pool keeps SQLite handles open
        # against a directory the next line deletes, and at ~1700 tests that surfaced as
        # "unable to open database file" in an unrelated module.
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        app_main.storage = self._orig_storage
        self.tempdir.cleanup()

    def as_(self, key):
        return {"x-api-key": key}

    def key_for(self, role):
        return app_main.storage.generate_api_key_for_tenant(self.tenant, role=role)["plaintext"]

    def create(self, **overrides):
        response = self.client.post(BASE, headers=self.as_(self.owner_key), json=body(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class CrudTest(_Base):

    def test_create_returns_the_secret_exactly_once(self):
        created = self.create()
        self.assertTrue(created["secret"], "creation must return the plaintext secret")
        fetched = self.client.get("{0}/{1}".format(BASE, created["id"]),
                                  headers=self.as_(self.owner_key)).json()
        self.assertEqual(fetched["secret"], "••••••••",
                         "the secret is readable after creation")

    def test_the_secret_is_generated_not_accepted_from_the_caller(self):
        """A secret the client chose is one that already existed in plaintext somewhere else."""
        created = self.client.post(BASE, headers=self.as_(self.owner_key),
                                   json={**body(), "secret": "chosen-by-caller"}).json()
        self.assertNotEqual(created.get("secret"), "chosen-by-caller")

    def test_list_read_update_delete(self):
        created = self.create()
        listed = self.client.get(BASE, headers=self.as_(self.owner_key)).json()
        self.assertEqual([s["id"] for s in listed], [created["id"]])

        patched = self.client.patch("{0}/{1}".format(BASE, created["id"]),
                                    headers=self.as_(self.owner_key),
                                    json={"is_active": False, "name": "paused"})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertFalse(patched.json()["is_active"])
        self.assertEqual(patched.json()["name"], "paused")

        deleted = self.client.delete("{0}/{1}".format(BASE, created["id"]),
                                     headers=self.as_(self.owner_key))
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get(BASE, headers=self.as_(self.owner_key)).json(), [])

    def test_the_event_type_list_is_served_rather_than_hardcoded_in_the_ui(self):
        response = self.client.get("{0}/event-types".format(BASE), headers=self.as_(self.owner_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["event_types"], list(webhook_dispatch.EVENT_TYPES))

    def test_patching_cannot_set_a_secret(self):
        created = self.create()
        self.client.patch("{0}/{1}".format(BASE, created["id"]),
                          headers=self.as_(self.owner_key), json={"secret": "sneaky"})
        stored = app_main.storage.get_webhook_subscription(
            created["id"], tenant_id=self.tenant, include_secret=True)
        self.assertNotEqual(stored["secret"], "sneaky")


class NegativeTest(_Base):
    """Not only the happy path -- the fifth definition-of-done item."""

    def test_an_unknown_event_type_is_refused_at_creation(self):
        response = self.client.post(BASE, headers=self.as_(self.owner_key),
                                    json=body(event_types=["decision.rejcted"]))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("unknown event type", response.text)

    def test_an_empty_event_selection_is_refused(self):
        response = self.client.post(BASE, headers=self.as_(self.owner_key),
                                    json=body(event_types=[]))
        self.assertEqual(response.status_code, 422)

    def test_a_filter_without_a_path_is_refused(self):
        response = self.client.post(BASE, headers=self.as_(self.owner_key),
                                    json=body(filters=[{"operator": "==", "value": 1}]))
        self.assertEqual(response.status_code, 422)

    def test_a_missing_subscription_is_404_on_every_verb(self):
        for call in (lambda: self.client.get("{0}/nope".format(BASE), headers=self.as_(self.owner_key)),
                     lambda: self.client.patch("{0}/nope".format(BASE), headers=self.as_(self.owner_key),
                                               json={"name": "x"}),
                     lambda: self.client.delete("{0}/nope".format(BASE), headers=self.as_(self.owner_key)),
                     lambda: self.client.post("{0}/nope/test".format(BASE), headers=self.as_(self.owner_key))):
            self.assertEqual(call().status_code, 404)

    def test_an_unauthenticated_caller_is_rejected(self):
        self.assertEqual(self.client.get(BASE).status_code, 401)

    def test_the_per_tenant_cap_is_enforced(self):
        """Each subscription multiplies the outbound requests one event produces."""
        original = webhook_dispatch.MAX_SUBSCRIPTIONS_PER_TENANT
        webhook_dispatch.MAX_SUBSCRIPTIONS_PER_TENANT = 2
        try:
            self.create(name="one")
            self.create(name="two")
            third = self.client.post(BASE, headers=self.as_(self.owner_key), json=body(name="three"))
            self.assertEqual(third.status_code, 422)
            self.assertIn("maximum", third.text)
        finally:
            webhook_dispatch.MAX_SUBSCRIPTIONS_PER_TENANT = original


class RbacAcrossAllFiveRolesTest(_Base):
    """The access each role should have, and no more."""

    READERS = ("owner", "admin", "policy_maker", "reviewer", "viewer")
    WRITERS = ("owner", "admin")

    def test_the_capability_is_mapped_EXPLICITLY_not_inherited_from_the_default(self):
        """Behaviourally this is indistinguishable, which is exactly why it needs a white-box
        assertion.

        rbac's unmapped-write default is already MANAGE_ACCESS (fail closed), so deleting the
        `/api/v1/webhook-subscriptions` entry from `_WRITE_PREFIX_CAPABILITY` changes NOTHING for
        any of the five roles -- a mutation proved every test in this class still passes without
        it. Relying on that is fragile in one specific way: the entry is what keeps the mapping
        correct if someone later adds a broader authoring prefix that swallows this path, or
        relaxes the default. The choice was deliberate, so it is asserted as a choice.
        """
        from app import rbac

        self.assertIn(("/api/v1/webhook-subscriptions", rbac.MANAGE_ACCESS),
                      rbac._WRITE_PREFIX_CAPABILITY,
                      "the capability for this prefix is only coming from the fail-closed "
                      "default; list it explicitly so a future prefix cannot lower it")
        self.assertEqual(rbac.required_capability("POST", "/api/v1/webhook-subscriptions"),
                         rbac.MANAGE_ACCESS)

    def test_the_inbound_webhook_prefix_does_not_swallow_this_one(self):
        """`/api/v1/webhooks` is an AUTHOR prefix and sits right next to this path. Prefix
        matching is `startswith`, so the two names being one character apart is worth a test
        rather than a glance."""
        from app import rbac

        self.assertEqual(rbac.required_capability("POST", "/api/v1/webhooks"), rbac.AUTHOR)
        self.assertEqual(rbac.required_capability("POST", "/api/v1/webhook-subscriptions"),
                         rbac.MANAGE_ACCESS)

    def test_every_role_can_read(self):
        self.create()
        for role in self.READERS:
            with self.subTest(role=role):
                key = self.owner_key if role == "owner" else self.key_for(role)
                self.assertEqual(self.client.get(BASE, headers=self.as_(key)).status_code, 200)

    def test_only_manage_access_roles_can_create(self):
        for role in self.READERS:
            with self.subTest(role=role):
                key = self.owner_key if role == "owner" else self.key_for(role)
                status = self.client.post(BASE, headers=self.as_(key), json=body()).status_code
                expected = 200 if role in self.WRITERS else 403
                self.assertEqual(status, expected,
                                 "{0} got {1} creating a subscription".format(role, status))

    def test_a_policy_maker_cannot_repoint_the_event_stream(self):
        """The specific escalation this capability choice exists to prevent. `policy_maker` is
        documented as unable to manage access, and a subscription URL is where workspace data
        goes."""
        created = self.create()
        author = self.key_for("policy_maker")
        patched = self.client.patch("{0}/{1}".format(BASE, created["id"]),
                                    headers=self.as_(author),
                                    json={"url": "https://attacker.example.com/collect"})
        self.assertEqual(patched.status_code, 403)
        unchanged = app_main.storage.get_webhook_subscription(created["id"], tenant_id=self.tenant)
        self.assertEqual(unchanged["url"], "https://hooks.example.com/rulemind",
                         "the refused patch still changed the destination")

    def test_no_role_below_manage_access_can_delete_or_test(self):
        created = self.create()
        for role in ("policy_maker", "reviewer", "viewer"):
            with self.subTest(role=role):
                key = self.key_for(role)
                self.assertEqual(self.client.delete("{0}/{1}".format(BASE, created["id"]),
                                                    headers=self.as_(key)).status_code, 403)
                self.assertEqual(self.client.post("{0}/{1}/test".format(BASE, created["id"]),
                                                  headers=self.as_(key)).status_code, 403)


class TenantIsolationTest(_Base):
    """Also covered generically by tests/test_tenant_isolation_matrix.py, where this entity is
    PROBED rather than exempted. Kept here too because a hand-written case states the property in
    terms of this entity, and the matrix states it in terms of a route shape."""

    def setUp(self):
        super().setUp()
        other = app_main.storage.create_tenant("Other Co")
        self.other = other["id"]
        self.other_key = app_main.storage.generate_api_key_for_tenant(
            self.other, role="owner")["plaintext"]

    def test_another_workspace_cannot_read_update_or_delete_it(self):
        created = self.create()
        path = "{0}/{1}".format(BASE, created["id"])
        # 404, never 403: a 403 confirms the object exists, which turns id enumeration into a
        # workspace census.
        self.assertEqual(self.client.get(path, headers=self.as_(self.other_key)).status_code, 404)
        self.assertEqual(self.client.patch(path, headers=self.as_(self.other_key),
                                           json={"name": "hijacked"}).status_code, 404)
        self.assertEqual(self.client.delete(path, headers=self.as_(self.other_key)).status_code, 404)

    def test_a_listing_never_includes_another_workspaces_subscriptions(self):
        self.create()
        self.assertEqual(self.client.get(BASE, headers=self.as_(self.other_key)).json(), [])

    def test_the_refused_access_had_no_side_effect(self):
        created = self.create()
        self.client.patch("{0}/{1}".format(BASE, created["id"]),
                          headers=self.as_(self.other_key), json={"name": "hijacked"})
        still = app_main.storage.get_webhook_subscription(created["id"], tenant_id=self.tenant)
        self.assertEqual(still["name"], "ops alerts")


class AuditTest(_Base):
    """Every mutation records an event carrying the authenticated ACTOR -- without the actor it
    is a log line, not an audit trail."""

    def events(self, entity_id):
        return [e for e in app_main.storage.list_audit_events(tenant_id=self.tenant)
                if e.get("entity_id") == entity_id]

    def test_creation_is_audited_with_the_actor(self):
        created = self.create()
        events = [e for e in self.events(created["id"])
                  if e["event_type"] == "webhook_subscription_created"]
        self.assertEqual(len(events), 1, "creation was not audited")
        metadata = events[0].get("metadata") or events[0].get("metadata_json") or {}
        self.assertTrue(metadata.get("actor_api_key_id"), "the audit event has no actor")
        self.assertEqual(metadata.get("actor_role"), "owner")

    def test_update_and_delete_are_audited(self):
        created = self.create()
        self.client.patch("{0}/{1}".format(BASE, created["id"]),
                          headers=self.as_(self.owner_key), json={"is_active": False})
        self.client.delete("{0}/{1}".format(BASE, created["id"]), headers=self.as_(self.owner_key))
        types = {e["event_type"] for e in self.events(created["id"])}
        self.assertIn("webhook_subscription_updated", types)
        self.assertIn("webhook_subscription_deleted", types)

    def test_the_audit_detail_names_the_destination(self):
        """An audit entry for an egress change is only useful if it says where."""
        created = self.create()
        event = [e for e in self.events(created["id"])
                 if e["event_type"] == "webhook_subscription_created"][0]
        self.assertIn("hooks.example.com", event["detail"])


if __name__ == "__main__":
    unittest.main()
