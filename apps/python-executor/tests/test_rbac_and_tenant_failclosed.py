"""Authorization and tenant resolution must fail CLOSED (Tier 3.3, 3.4).

3.3 rbac.required_capability returned AUTHOR for any write it did not recognise. So every new
    mutating prefix was auto-granted to policy_maker the moment someone added it -- the map was
    permissive by omission. /scim/v2 is the proof it actually happened: SCIM user provisioning,
    including DEACTIVATING the workspace owner, was reachable at AUTHOR by a role documented as
    "cannot deploy or manage access". /api/admin/v1 and /api/v1/auth fell through the same way.

3.4 main.active_tenant_id fell back to storage.default_tenant_id whenever no tenant context
    existed. Any route outside the middleware, any background task, any WebSocket therefore read
    AND WROTE the FIRST tenant's data. Separately, 15 storage writers resolved
    `tenant_id or payload.get("tenant_id")`, putting a request-body field ahead of the
    authenticated context.
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

from fastapi import HTTPException  # noqa: E402

import app.main as app_main  # noqa: E402
from app.rbac import (  # noqa: E402
    AUTHOR, DECIDE, MANAGE_ACCESS, ROLE_CAPABILITIES, required_capability,
)
from app.storage import Storage  # noqa: E402


class UnknownWriteFailsClosedTest(unittest.TestCase):
    def test_an_unmapped_write_prefix_is_admin_only(self):
        self.assertEqual(required_capability("POST", "/api/v1/some-future-feature"),
                         MANAGE_ACCESS,
                         "an unmapped mutating route is auto-granted to policy_maker")

    def test_policy_maker_cannot_reach_an_unmapped_write(self):
        """The concrete consequence, stated as a role check rather than a constant comparison."""
        needed = required_capability("POST", "/api/v1/some-future-feature")
        self.assertNotIn(needed, ROLE_CAPABILITIES["policy_maker"])

    def test_scim_provisioning_requires_manage_access(self):
        for method, path in [("POST", "/scim/v2/Users"), ("PUT", "/scim/v2/Users/u1"),
                             ("PATCH", "/scim/v2/Users/u1"), ("DELETE", "/scim/v2/Users/u1")]:
            with self.subTest(path=path):
                self.assertEqual(required_capability(method, path), MANAGE_ACCESS)

    def test_a_policy_maker_cannot_deactivate_a_member(self):
        self.assertNotIn(required_capability("DELETE", "/scim/v2/Users/owner"),
                         ROLE_CAPABILITIES["policy_maker"],
                         "policy_maker can deactivate members, the owner included")

    def test_superadmin_routes_require_manage_access(self):
        self.assertEqual(required_capability("POST", "/api/admin/v1/tenants"), MANAGE_ACCESS)

    def test_authoring_routes_are_still_author(self):
        """The regression guard: failing closed must not lock authors out of authoring."""
        for path in ["/api/v1/rules", "/api/v1/variables", "/api/v1/policies",
                     "/api/v1/scorecards", "/api/v1/decision-tables", "/api/v1/connectors",
                     "/api/v1/ai/generate-rule", "/api/v1/collab/merge", "/api/v1/import",
                     "/api/v1/evaluations", "/api/v1/reports/generate",
                     "/api/v1/onboarding/load-samples"]:
            with self.subTest(path=path):
                self.assertEqual(required_capability("POST", path), AUTHOR,
                                 "{0} became admin-only".format(path))

    def test_evaluation_routes_are_still_decide(self):
        for path in ["/api/v1/decide", "/api/v1/executions/e1/resume",
                     "/api/v1/decisions/d1/replay", "/api/v1/models/m1/predict"]:
            with self.subTest(path=path):
                self.assertEqual(required_capability("POST", path), DECIDE)

    def test_every_mutating_route_in_the_app_is_classified_deliberately(self):
        """Anti-rot: a route reaching the fail-closed default is admin-only, which is safe but
        usually unintended. Listing them here means adding a route makes you choose."""
        unclassified = []
        for route in app_main.app.routes:
            writes = (getattr(route, "methods", set()) or set()) - {"GET", "HEAD", "OPTIONS"}
            path = getattr(route, "path", "")
            if not writes or not path:
                continue
            if required_capability(sorted(writes)[0], path) != MANAGE_ACCESS:
                continue
            # These are deliberately MANAGE_ACCESS.
            if path.startswith(("/api/v1/access", "/api/v1/settings", "/api/v1/branding",
                                "/api/v1/reports/email-config", "/scim/v2", "/api/admin/v1",
                                "/api/v1/auth", "/api/mobile/v1/auth", "/api/v1/models",
                                # Outbound webhook subscriptions (P03). Deliberate, and NOT the
                                # same class as the inbound /api/v1/webhooks entity beside it,
                                # which is AUTHOR: a subscription is standing egress
                                # configuration that sends this workspace's decision data to a
                                # third party until someone removes it. A policy_maker can
                                # already POST anywhere from an action step, so this is not
                                # about reaching the network -- it is that the role documented
                                # as "cannot manage access" should not be able to repoint the
                                # workspace's event stream.
                                "/api/v1/webhook-subscriptions")):
                continue
            unclassified.append(path)
        self.assertEqual(unclassified, [],
                         "these mutating routes hit the fail-closed default and are now "
                         "admin-only; map them explicitly in rbac.py")


class TenantResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "tenant.db"))
        self._prev_auth = os.environ.get("AUTH_MODE")
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        if self._prev_auth is None:
            os.environ.pop("AUTH_MODE", None)
        else:
            os.environ["AUTH_MODE"] = self._prev_auth
        self.tempdir.cleanup()

    def test_no_tenant_context_raises_instead_of_serving_the_first_tenant(self):
        with self.assertRaises(HTTPException) as caught:
            app_main.active_tenant_id()
        self.assertEqual(caught.exception.status_code, 500)

    def test_auth_disabled_still_resolves_to_the_default_tenant(self):
        """A single-tenant, auth-disabled deployment legitimately has one tenant. Removing this
        would break every AUTH_MODE=none dev and self-hosted setup."""
        os.environ["AUTH_MODE"] = "none"
        self.assertEqual(app_main.active_tenant_id(), app_main.storage.default_tenant_id)


class TenantPrecedenceTest(unittest.TestCase):
    """The authenticated context must outrank a request-body tenant_id."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "prec.db"))
        self.first = self.storage.default_tenant_id
        self.second = self.storage.create_tenant("Second Co")["id"]

    def tearDown(self):
        self.tempdir.cleanup()

    def test_the_auth_context_beats_a_body_supplied_tenant(self):
        from app.context import reset_current_tenant_id, set_current_tenant_id

        token = set_current_tenant_id(self.second)
        try:
            resolved = self.storage._tenant_id(None, payload_tenant=self.first)
        finally:
            reset_current_tenant_id(token)
        self.assertEqual(resolved, self.second,
                         "a body-supplied tenant_id outranked the authenticated context")

    def test_an_explicit_argument_still_wins(self):
        """Internal callers that know the tenant must keep control."""
        self.assertEqual(self.storage._tenant_id(self.second, payload_tenant=self.first),
                         self.second)

    def test_a_payload_tenant_is_still_honoured_with_no_other_context(self):
        """Batch import and WAL replay carry the tenant in the record; they must keep working."""
        self.assertEqual(self.storage._tenant_id(None, payload_tenant=self.second), self.second)


class WebhookTenantResolutionTest(unittest.TestCase):
    """Webhooks were broken for every tenant but the first (found by T3.4's fail-closed change).

    POST /api/v1/webhooks/{id} is unauthenticated by design -- a third party posts to it and the
    HMAC signature is the authentication -- so there is no auth context. trigger_webhook looked
    the webhook up with the TENANT-SCOPED get_webhook(), which with no context resolves to the
    FIRST tenant. Every other tenant's webhook therefore returned None, surfacing as a 404
    "Webhook not found" with nothing to say why. Silent, and invisible on a single-tenant demo.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "wh.db"))
        self.first = self.storage.default_tenant_id
        self.second = self.storage.create_tenant("Second Co")["id"]
        self.policy_id = self.storage.list_policies(tenant_id=self.first)[0]["id"]

    def tearDown(self):
        self.tempdir.cleanup()

    def _webhook(self, tenant_id):
        import uuid

        wid = str(uuid.uuid4())
        return self.storage.create_webhook(
            {"id": wid, "policy_id": self.policy_id, "payload_mapping": {},
             "endpoint_path": "/hooks/{0}".format(wid)}, tenant_id=tenant_id)

    def test_a_second_tenants_webhook_is_resolvable(self):
        webhook = self._webhook(self.second)
        found = self.storage.get_webhook_for_trigger(webhook["id"])
        self.assertIsNotNone(found, "a non-default tenant's webhook is unreachable")
        self.assertEqual(found["tenant_id"], self.second)

    def test_the_tenant_scoped_lookup_is_still_scoped(self):
        """The receiver's lookup is deliberately tenant-agnostic; the AUTHENTICATED one must not
        be, or an author could read another tenant's webhook (and its secret)."""
        webhook = self._webhook(self.second)
        self.assertIsNone(self.storage.get_webhook(webhook["id"], tenant_id=self.first),
                          "the tenant-scoped lookup leaked another tenant's webhook")

    def test_the_trigger_lookup_carries_its_own_tenant(self):
        """Downstream work must use the record's tenant, not ambient context."""
        for tenant in (self.first, self.second):
            with self.subTest(tenant=tenant):
                webhook = self._webhook(tenant)
                self.assertEqual(
                    self.storage.get_webhook_for_trigger(webhook["id"])["tenant_id"], tenant)

    def test_an_unknown_webhook_id_is_still_not_found(self):
        self.assertIsNone(self.storage.get_webhook_for_trigger("00000000-0000-0000-0000-000000000000"))

    def test_triggering_a_second_tenants_webhook_actually_runs_its_policy(self):
        """End-to-end through trigger_webhook -- the storage tests above pass whichever lookup
        the caller uses, so this is what proves the caller was changed."""
        import asyncio

        from app import webhooks

        # The second tenant needs a policy of its own to run.
        policy = self.storage.get_policy(self.policy_id, tenant_id=self.first)
        self.storage.create_policy(
            {"id": "pol_second", "name": "Second Policy", "defaultOutcome": "review",
             "steps": [{"id": "s", "type": "outcome", "ref_id": "approve",
                        "config": {"outcome": "approve"}, "label": "approve"}]},
            tenant_id=self.second)
        import uuid

        wid = str(uuid.uuid4())
        self.storage.create_webhook(
            {"id": wid, "policy_id": "pol_second", "payload_mapping": {},
             "endpoint_path": "/hooks/{0}".format(wid)}, tenant_id=self.second)

        result = asyncio.run(webhooks.trigger_webhook(self.storage, wid, {"age": 30}))
        self.assertIsNotNone(result, "the second tenant's webhook did not execute")
        self.assertEqual(result.get("outcome"), "approve", result)
        self.assertIsNotNone(policy)


if __name__ == "__main__":
    unittest.main()
