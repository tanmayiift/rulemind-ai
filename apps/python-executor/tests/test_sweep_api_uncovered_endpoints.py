"""The endpoints that no test, e2e spec or QA suite mentioned.

Enumerated mechanically rather than by reading: every `@router.<verb>("...")` across
`app/routers/`, cross-referenced against every file under `tests/`, `e2e/`, `qa/` and `test/`.
That found **9 of 272** with no mention anywhere -- these.

Two of them are more interesting than "an uncovered GET":

`GET /api/v1/variables/graph` (authoring.py) and `GET /api/v1/reviews/stats` (operations.py) take
NO `request` parameter and call storage with NO tenant_id, unlike every other endpoint on those
routers. That is not a leak -- `Storage._tenant_id` falls back to the `get_current_tenant_id()`
contextvar the middleware sets -- but nothing anywhere proved it, and the difference between
"scoped by an explicit argument" and "scoped by an ambient contextvar" is exactly the kind of
thing that survives a refactor as a cross-tenant read. So both get a real two-tenant isolation
test rather than a smoke test.

The SCIM discovery endpoints are required BY THE SPEC (RFC 7644 §4): a client uses them to learn
what the server supports before provisioning anything.
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
from tests._storage_global import preserve_storage_global  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "uncovered.db"))
        self.client = TestClient(app_main.app)
        self.tenant_a = app_main.storage.default_tenant_id
        self.headers_a = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_b = app_main.storage.create_tenant("Sweep Uncovered Co")["id"]
        self.headers_b = {
            "x-api-key": app_main.storage.generate_api_key_for_tenant(self.tenant_b)["plaintext"]
        }

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()


class TenantScopedWithoutARequestObjectTests(_Case):
    """The two endpoints that resolve their tenant from the contextvar rather than an argument."""

    def test_the_variable_graph_only_shows_this_tenants_variables(self):
        app_main.storage.create_variable(
            {"id": "var_only_a", "name": "only_a", "category": "custom",
             "source_id": "s", "code": "x = 1"},
            tenant_id=self.tenant_a)
        app_main.storage.create_variable(
            {"id": "var_only_b", "name": "only_b", "category": "custom",
             "source_id": "s", "code": "x = 2"},
            tenant_id=self.tenant_b)

        graph_a = self.client.get("/api/v1/variables/graph", headers=self.headers_a).json()
        ids_a = {node.get("id") for node in graph_a.get("nodes", [])}
        self.assertIn("var_only_a", ids_a, "the caller's own variable must be in the graph")
        self.assertNotIn(
            "var_only_b", ids_a,
            "another workspace's variable reached this graph -- the endpoint takes no request "
            "and passes no tenant_id, so it depends entirely on the contextvar being set")

        graph_b = self.client.get("/api/v1/variables/graph", headers=self.headers_b).json()
        ids_b = {node.get("id") for node in graph_b.get("nodes", [])}
        self.assertIn("var_only_b", ids_b)
        self.assertNotIn("var_only_a", ids_b)

    def test_review_stats_counts_only_this_tenants_queue(self):
        for tenant, queue in ((self.tenant_a, "queue_a"), (self.tenant_b, "queue_b")):
            app_main.storage.create_review_task(
                {"execution_id": "exec-{0}".format(queue), "queue": queue,
                 "status": "pending"},
                tenant_id=tenant)

        stats_a = self.client.get("/api/v1/reviews/stats", headers=self.headers_a).json()
        self.assertEqual(stats_a["pending"], 1)
        self.assertIn("queue_a", stats_a["queues"])
        self.assertNotIn(
            "queue_b", stats_a["queues"],
            "another workspace's review queue appeared in this tenant's stats")

        stats_b = self.client.get("/api/v1/reviews/stats", headers=self.headers_b).json()
        self.assertEqual(stats_b["pending"], 1)
        self.assertIn("queue_b", stats_b["queues"])
        self.assertNotIn("queue_a", stats_b["queues"])


class DiscoveryEndpointTests(_Case):
    """Closed lists the UI and SCIM clients read so they do not hardcode and drift."""

    def test_webhook_event_types_matches_the_dispatcher_itself(self):
        from app import webhook_dispatch

        body = self.client.get("/api/v1/webhook-subscriptions/event-types",
                               headers=self.headers_a).json()
        self.assertEqual(
            sorted(body["event_types"]), sorted(webhook_dispatch.EVENT_TYPES),
            "the endpoint exists so the UI need not hardcode this list; if it can drift from "
            "the dispatcher's own tuple it has no purpose")

    def test_predictor_source_types_matches_the_module_itself(self):
        from app import predictors

        body = self.client.get("/api/v1/predictors/source-types", headers=self.headers_a).json()
        self.assertEqual(sorted(body["source_types"]), sorted(predictors.SOURCE_TYPES))

    def test_a_source_type_from_that_list_is_actually_accepted(self):
        """The list and the validator must agree -- the /predictors page builds its dropdown
        from this endpoint, so a value here that POST rejects is a dead option in the UI."""
        listed = self.client.get("/api/v1/predictors/source-types",
                                 headers=self.headers_a).json()["source_types"]
        self.assertIn("outcome", listed)
        created = self.client.post("/api/v1/predictors", headers=self.headers_a, json={
            "name": "sweep-outcome", "source_type": "outcome"})
        self.assertEqual(created.status_code, 200, created.text)

    def test_scim_resource_types_is_a_valid_list_response(self):
        response = self.client.get("/scim/v2/ResourceTypes", headers=self.headers_a)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("urn:ietf:params:scim:api:messages:2.0:ListResponse", body["schemas"])
        self.assertTrue(any(r.get("id") == "User" for r in body["Resources"]))
        self.assertEqual(
            response.headers.get("content-type", "").split(";")[0], "application/scim+json",
            "RFC 7644 requires the SCIM media type; a client that content-negotiates will "
            "refuse application/json")

    def test_scim_schemas_describes_the_user_resource(self):
        response = self.client.get("/scim/v2/Schemas", headers=self.headers_a)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(
            any(r.get("id") == "urn:ietf:params:scim:schemas:core:2.0:User"
                for r in body["Resources"]),
            "a provisioning client reads this before it sends anything")


class AdminSessionEndpointTests(_Case):
    """The platform-admin session pair. Neither had any coverage, including the refusal path."""

    def test_admin_me_without_a_cookie_is_refused(self):
        response = self.client.get("/api/admin/v1/auth/me")
        self.assertNotEqual(
            response.status_code, 200,
            "/api/admin/v1 is the SUPERADMIN surface; it must not answer an anonymous caller")
        self.assertIn(response.status_code, (401, 403))

    def test_a_workspace_api_key_does_not_authenticate_the_admin_surface(self):
        """The control that makes the refusal above meaningful: a perfectly valid tenant key is
        still not an admin credential."""
        response = self.client.get("/api/admin/v1/auth/me", headers=self.headers_a)
        self.assertNotEqual(response.status_code, 200)

    def test_logout_clears_the_admin_cookie(self):
        response = self.client.post("/api/admin/v1/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok", True))
        # The point of logout is the Set-Cookie that expires the session, not the JSON body.
        from app.auth import JWT_COOKIE_NAME

        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn(JWT_COOKIE_NAME, set_cookie,
                      "logout must expire the session cookie; the JSON body alone logs nobody out")
        self.assertIn("max-age=0", set_cookie.lower(),
                      "the cookie must be expired, not merely re-set")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
