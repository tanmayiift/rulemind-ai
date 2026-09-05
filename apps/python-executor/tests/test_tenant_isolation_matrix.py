"""Cross-tenant isolation, parameterised over the live route table (Track B5).

Two hand-written tests covered isolation before this: one for signups and one for decision
tables. Both are correct and both share the defect that matters -- they name one entity, so
every router added since was covered by nobody, and every router added next would be too.

This suite is built the other way round. It DISCOVERS the app's collection endpoints from
`app.routes` at run time, so a new router with a `/api/v1/<thing>` + `/api/v1/<thing>/{id}`
pair is picked up automatically. A collection that is neither probed nor explicitly exempted
fails `test_every_collection_is_classified`, which is the mechanism that keeps this from
rotting the way a hand-maintained list does.

The probes assert 404 rather than 403 on every cross-tenant access. That distinction is the
substance of the test, not pedantry: 403 confirms the object EXISTS, which leaks the very fact
a tenant boundary is supposed to hide -- an attacker can enumerate another workspace's policy
ids by watching 403s where they expected 404s.

DISCOVERY HAD A BLIND SPOT (T3.2). `discover_collections` matches only
`/api/v1/<name>` with an `/{id}` sibling, so `/ws/v1`, `/scim/v2`, `/api/admin/v1` and every
nested route were invisible to it -- and therefore invisible to
`test_every_collection_is_classified`, the anti-rot tripwire, which is exactly the class of
route where isolation was actually broken (the collab WebSocket was unauthenticated and keyed
without a tenant at all). `discover_mutating_routes` below widens the sweep to the WHOLE route
table: every mutating route must be probed here, covered by a named suite, or exempted with a
stated reason.
"""
from __future__ import annotations

import base64
import os
import pickle
import re
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
# Not optional: middleware.py early-returns on AUTH_MODE=none, which would skip the API-key
# check entirely. A tenant-isolation suite running without auth proves nothing at all.
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


class _Predictor:
    """Minimal object satisfying the model upload's `predict()` check."""

    def predict(self, rows):
        return [0 for _ in rows]

    def predict_proba(self, rows):
        return [[0.5, 0.5] for _ in rows]


def discover_collections(app) -> dict:
    """Every `/api/v1/<name>` that also has a `/api/v1/<name>/{id}` sibling, with its verbs.

    Read off the live router table rather than hard-coded, so this suite's coverage grows with
    the app instead of drifting behind it.
    """
    verbs: dict = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = (getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        if path and methods:
            verbs.setdefault(path, set()).update(methods)

    found = {}
    for path, methods in verbs.items():
        match = re.fullmatch(r"/api/v1/([a-z0-9\-]+)", path)
        if not match:
            continue
        item = [p for p in verbs if re.fullmatch(re.escape(path) + r"/\{[a-z_]+\}", p)]
        if item:
            found[match.group(1)] = {
                "collection": path,
                "item": item[0],
                "collection_methods": methods,
                "item_methods": verbs[item[0]],
            }
    return found


def discover_mutating_routes(app) -> set:
    """EVERY route in the app with a state-changing method, plus every WebSocket.

    Deliberately unfiltered, unlike discover_collections: the routes that turned out to have
    real isolation defects were the ones a `/api/v1/<name>` pattern could never match.
    """
    found = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path:
            continue
        methods = (getattr(route, "methods", set()) or set()) - {"GET", "HEAD", "OPTIONS"}
        # A WebSocket route has no `methods`; it is a bidirectional write channel and belongs in
        # this sweep. The collab socket accepted `edit` and `restore` from anyone.
        is_websocket = "websocket" in type(route).__name__.lower()
        if methods or is_websocket:
            found.add(path)
    return found


# Every mutating route must be accounted for by exactly one of:
#   * a CRUD fixture in _fixtures() (the matrix probes it),
#   * a prefix here, naming the suite that covers it or the reason it needs no tenant probe.
# A route matching nothing fails test_every_mutating_route_is_classified.
MUTATING_ROUTE_COVERAGE = {
    "/ws/v1/collab": "tests/test_collab_websocket_authz.py -- authenticates before accept() and "
                     "asserts the hub is keyed by (tenant_id, doc_id)",
    "/api/v1/collab": "tests/test_collab_websocket_authz.py -- the REST siblings are probed "
                      "cross-tenant there",
    "/scim/v2": "probed below in ScimIsolationTest; also gated at MANAGE_ACCESS by rbac",
    "/api/admin/v1": "superadmin surface, deliberately OUTSIDE tenant scope -- it operates ON "
                     "tenants. Its own authorization is covered by the admin auth tests",
    "/api/v1/webhooks/": "the receiver is unauthenticated by design (HMAC-signed); tenant comes "
                         "from the webhook record, covered in test_rbac_and_tenant_failclosed.py",
    "/api/v1/decide": "evaluation, not storage: tenant comes from the auth context and the "
                      "policy lookup is tenant-scoped (probed via policies above)",
    "/api/v1/test": "console evaluation against a caller-supplied entity, no cross-tenant object",
    "/sdk/v1": "SDK evaluation + sync; tenant from the auth context, covered by the SDK suites",
    "/api/mobile/v1": "mobile session establishment; identity routes, covered by the auth suites",
    "/api/v1/auth": "establishes identity before one exists -- no tenant to cross",
    "/api/v1/onboarding": "creates a NEW tenant (signup) or seeds the caller's own",
    "/api/v1/import": "bulk-writes into the caller's own tenant from the auth context",
    "/api/v1/executions": "resumes an execution looked up in the caller's tenant",
    "/api/v1/decisions": "replays a decision looked up in the caller's tenant",
    "/api/v1/workflows": "loop-debug / callbacks against the caller's own tenant",
    "/api/v1/ai": "AI drafting; produces a draft for the caller, stores nothing cross-tenant",
    "/api/v1/analytics": "recompute over the caller's own decisions",
    "/api/v1/access": "member management within the caller's tenant, gated at MANAGE_ACCESS",
    "/api/v1/settings": "the caller's own tenant settings, gated at MANAGE_ACCESS",
    "/api/v1/deploy": "promotes within the caller's tenant, gated at DEPLOY",
    "/api/v1/reviews": "review tasks are created by the executor, not by a POST (see EXEMPT)",
    "/api/v1/evaluations": "runs against an uploaded model (see EXEMPT)",
    "/api/v1/reports": "generates from the caller's own decisions",
    "/api/v1/schedules": "cron over the caller's own policies (CRUD pair probed above)",
    "/api/v1/experiments": "CRUD pair probed above",
    "/api/v1/models": "CRUD pair probed above; management gated at MANAGE_ACCESS",
}


# Collections deliberately not probed, each with the reason. Anything NOT listed here and not
# probed fails the classification test -- so a future router cannot be silently uncovered.
EXEMPT = {
    "providers": "read-only catalogue of AI providers; global, not tenant-owned, so there is "
                 "no cross-tenant object to leak",
    "reviews": "human-review tasks are created by the executor pausing on a review gate, not "
               "by a POST; isolation for them is covered where that gate is exercised",
    "evaluations": "POST /evaluations RUNS an evaluation against an uploaded model and rejects "
                   "an empty dataset, so it cannot be created as an inert fixture here",
}


class TenantIsolationMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.collections = discover_collections(app_main.app)
        cls.mutating_routes = discover_mutating_routes(app_main.app)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "iso.db"))
        self.client = TestClient(app_main.app)

        self.a_tenant = app_main.storage.default_tenant_id
        self.a = {"x-api-key": app_main.storage.default_api_key or ""}
        other = app_main.storage.create_tenant("Intruder Inc")
        self.b_tenant = other["id"]
        self.b = {"x-api-key": app_main.storage.generate_api_key_for_tenant(other["id"])["plaintext"]}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    # ── fixtures owned by tenant A ──────────────────────────────────────────

    def _create(self, collection: str, body: dict, headers=None):
        res = self.client.post(f"/api/v1/{collection}", headers=headers or self.a, json=body)
        self.assertLess(res.status_code, 300, f"fixture for {collection} failed: {res.text[:300]}")
        return res.json()

    def _fixtures(self) -> dict:
        """One object per probed collection, all owned by tenant A. Built in dependency order:
        rules and scorecards need a real variable, schedules and webhooks a real policy."""
        variable = self._create("variables", {
            "name": "iso_var", "source": "custom", "category": "custom",
            "source_id": "custom", "code": "x = 1"})
        policy = self._create("policies", {
            "name": "iso_policy", "steps": [{"id": "s1", "type": "rule", "ref": "r1"}]})
        made = {"variables": variable, "policies": policy}

        made["rules"] = self._create("rules", {
            "name": "iso_rule", "outcome": "approve",
            "nodes": [{"id": "n1", "type": "condition", "variable": variable["id"],
                       "operator": ">", "value": "1"},
                      {"id": "n2", "type": "approve"}]})
        made["scorecards"] = self._create("scorecards", {
            "name": "iso_sc", "attributes": [],
            "bins": [{"variable_id": variable["id"],
                      "ranges": [{"min": 0, "max": 1, "points": 10}]}]})
        made["schedules"] = self._create("schedules", {
            "name": "iso_sched", "cron_expression": "0 0 * * *", "action": "report",
            "policy_id": policy["id"]})
        made["webhooks"] = self._create("webhooks", {
            "name": "iso_hook", "url": "https://example.com/hook",
            "events": ["decision"], "policy_id": policy["id"]})
        made["connectors"] = self._create("connectors", {
            "name": "iso_conn", "type": "rest", "config": {}})
        made["decision-tables"] = self._create("decision-tables", {
            "name": "iso_dt", "inputs": [], "outputs": [], "rows": []})
        made["experiments"] = self._create("experiments", {
            "name": "iso_exp", "policy_id": policy["id"], "variants": []})
        made["predictors"] = self._create("predictors", {
            "name": "iso_pred", "source_type": "variable", "source_id": variable["id"]})
        made["webhook-subscriptions"] = self._create("webhook-subscriptions", {
            "name": "iso_sub", "url": "https://example.com/events",
            "event_types": ["decision.rejected"]})
        made["reports"] = self._create("reports", {"name": "iso_report", "type": "decisions"})
        made["models"] = self._create("models", {
            "name": "iso_model", "type": "sklearn",
            "model_base64": base64.b64encode(pickle.dumps(_Predictor())).decode()})
        return made

    @staticmethod
    def _id_of(obj: dict) -> str:
        for key in ("id", "task_id", "model_id"):
            if obj.get(key):
                return str(obj[key])
        raise AssertionError(f"fixture has no id: {sorted(obj)[:10]}")

    # ── the matrix ──────────────────────────────────────────────────────────

    def test_every_collection_is_classified(self):
        """The mechanism that stops this suite rotting. A new router adding a CRUD pair must be
        either probed or exempted with a stated reason -- it cannot be silently uncovered."""
        probed = set(self._fixtures())
        unclassified = set(self.collections) - probed - set(EXEMPT)
        self.assertFalse(
            unclassified,
            f"collection(s) {sorted(unclassified)} are neither probed nor exempted. Add a "
            f"fixture in _fixtures(), or an entry in EXEMPT stating why isolation cannot be "
            f"probed there.")

    def test_every_mutating_route_is_classified(self):
        """The widened tripwire (T3.2).

        The original discovery matched only `/api/v1/<name>` CRUD pairs, so `/ws/v1`, `/scim/v2`,
        `/api/admin/v1` and every nested route were invisible to it -- and the collab WebSocket,
        which had NO auth and NO tenant key, sat squarely in that blind spot. Any mutating route
        that matches neither a probed CRUD collection nor a MUTATING_ROUTE_COVERAGE prefix fails
        here, so the next such route cannot arrive unnoticed.
        """
        probed_prefixes = tuple("/api/v1/{0}".format(name) for name in self._fixtures())
        exempt_prefixes = tuple("/api/v1/{0}".format(name) for name in EXEMPT)
        covered_prefixes = tuple(MUTATING_ROUTE_COVERAGE)
        unclassified = sorted(
            path for path in self.mutating_routes
            if not path.startswith(probed_prefixes + exempt_prefixes + covered_prefixes))
        self.assertEqual(
            unclassified, [],
            "mutating route(s) {0} are not accounted for. Probe them, or add a prefix to "
            "MUTATING_ROUTE_COVERAGE stating which suite covers them and why.".format(unclassified))

    def test_the_route_coverage_map_has_no_stale_entries(self):
        """A prefix naming routes that no longer exist is stale documentation posing as a
        decision -- the same failure mode test_exemptions_still_exist guards for collections."""
        stale = sorted(prefix for prefix in MUTATING_ROUTE_COVERAGE
                       if not any(path.startswith(prefix) for path in self.mutating_routes))
        self.assertEqual(stale, [],
                         "MUTATING_ROUTE_COVERAGE names prefixes with no live routes: {0}".format(stale))

    def test_the_widened_discovery_actually_sees_the_blind_spot(self):
        """Guard on the guard. If discovery silently narrowed back to /api/v1 CRUD, the test
        above would pass by finding nothing -- so assert the routes it missed are now visible."""
        self.assertTrue(any(p.startswith("/ws/v1") for p in self.mutating_routes),
                        "WebSocket routes are invisible to discovery")
        self.assertTrue(any(p.startswith("/scim/v2") for p in self.mutating_routes),
                        "/scim/v2 is invisible to discovery")
        self.assertTrue(any(p.startswith("/api/admin/v1") for p in self.mutating_routes),
                        "/api/admin/v1 is invisible to discovery")
        self.assertGreater(len(self.mutating_routes), len(self.collections) * 2,
                           "discovery is not sweeping the whole route table")

    def test_exemptions_still_exist(self):
        """An exemption for a collection that has since been removed is stale documentation
        pretending to be a decision."""
        stale = set(EXEMPT) - set(self.collections)
        self.assertFalse(stale, f"EXEMPT names collection(s) that no longer exist: {sorted(stale)}")

    def test_listing_never_leaks_another_tenants_objects(self):
        fixtures = self._fixtures()
        for name, obj in fixtures.items():
            with self.subTest(collection=name):
                res = self.client.get(f"/api/v1/{name}", headers=self.b)
                self.assertEqual(res.status_code, 200, res.text[:200])
                body = res.json()
                rows = body if isinstance(body, list) else body.get("items", body.get("data", []))
                ids = {str(r.get("id")) for r in rows if isinstance(r, dict)}
                self.assertNotIn(self._id_of(obj), ids,
                                 f"{name}: tenant B's listing contains tenant A's object")

    def test_reading_another_tenants_object_is_404_not_403(self):
        """403 would confirm the object exists, which lets an attacker enumerate another
        workspace's ids by watching which unknown ids come back 403 instead of 404."""
        fixtures = self._fixtures()
        for name, obj in fixtures.items():
            with self.subTest(collection=name):
                res = self.client.get(f"/api/v1/{name}/{self._id_of(obj)}", headers=self.b)
                self.assertEqual(res.status_code, 404,
                                 f"{name}: expected 404, got {res.status_code} {res.text[:160]}")

    def test_writing_to_another_tenants_object_is_refused(self):
        fixtures = self._fixtures()
        for name, obj in fixtures.items():
            methods = self.collections[name]["item_methods"]
            if "PUT" not in methods:
                continue
            with self.subTest(collection=name):
                res = self.client.put(f"/api/v1/{name}/{self._id_of(obj)}", headers=self.b,
                                      json={"name": "owned"})
                self.assertIn(res.status_code, (403, 404, 422),
                              f"{name}: tenant B's write was accepted ({res.status_code})")
                # And the object is untouched -- a refused status with a completed side effect
                # would be the worst of both.
                after = self.client.get(f"/api/v1/{name}/{self._id_of(obj)}", headers=self.a)
                if after.status_code == 200:
                    self.assertNotEqual(after.json().get("name"), "owned",
                                        f"{name}: refused the request but applied the change")

    def test_deleting_another_tenants_object_does_not_delete_it(self):
        """The one with a destructive failure mode: a DELETE that returns 404 while actually
        removing the row is far worse than one that returns 200."""
        fixtures = self._fixtures()
        for name, obj in fixtures.items():
            if "DELETE" not in self.collections[name]["item_methods"]:
                continue
            with self.subTest(collection=name):
                object_id = self._id_of(obj)
                self.client.delete(f"/api/v1/{name}/{object_id}", headers=self.b)
                survives = self.client.get(f"/api/v1/{name}/{object_id}", headers=self.a)
                self.assertEqual(survives.status_code, 200,
                                 f"{name}: tenant B's DELETE removed tenant A's object")

    def test_an_api_key_is_bound_to_exactly_one_tenant(self):
        """The premise everything above rests on. If the two keys resolved to the same tenant,
        every assertion in this file would pass while proving nothing."""
        self.assertNotEqual(self.a_tenant, self.b_tenant)
        a_policy = self._create("policies", {
            "name": "a_only", "steps": [{"id": "s1", "type": "rule", "ref": "r1"}]})
        as_a = self.client.get(f"/api/v1/policies/{a_policy['id']}", headers=self.a)
        self.assertEqual(as_a.status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/policies/{a_policy['id']}",
                                         headers=self.b).status_code, 404)


class ScimIsolationTest(unittest.TestCase):
    """Cross-tenant probes for /scim/v2, which the widened discovery surfaced (T3.2).

    SCIM is the user-PROVISIONING api: a leak here is not "read another workspace's policy", it
    is "enumerate and deactivate another workspace's people". It sat in the old discovery's blind
    spot -- no `/api/v1/<name>` pattern matches it -- so nothing verified its scoping.

    Like the matrix above, these assert 404 rather than 403: a 403 would confirm the user exists,
    which is exactly the fact a tenant boundary must hide.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "scim.db"))
        self.client = TestClient(app_main.app)
        self.a_tenant = app_main.storage.default_tenant_id
        self.a = {"x-api-key": app_main.storage.default_api_key or ""}
        b = app_main.storage.create_tenant("Tenant B")
        self.b_tenant = b["id"]
        self.b = {"x-api-key": app_main.storage.generate_api_key_for_tenant(b["id"])["plaintext"]}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _member_of_a(self):
        return app_main.storage.create_member(
            self.a_tenant, "alice@tenant-a.example", "Alice", role="viewer")

    def test_the_two_tenants_are_distinct(self):
        """The premise. Without this every assertion below could pass while proving nothing."""
        self.assertNotEqual(self.a_tenant, self.b_tenant)

    def test_b_cannot_read_as_members(self):
        member = self._member_of_a()
        res = self.client.get("/scim/v2/Users/{0}".format(member["id"]), headers=self.b)
        self.assertEqual(res.status_code, 404, res.text[:200])

    def test_b_cannot_list_as_members(self):
        member = self._member_of_a()
        res = self.client.get("/scim/v2/Users", headers=self.b)
        self.assertEqual(res.status_code, 200, res.text[:200])
        self.assertNotIn(member["id"], res.text)
        self.assertNotIn("alice@tenant-a.example", res.text)

    def test_b_cannot_deactivate_as_member(self):
        """The deprovisioning path -- the one that can lock a workspace out of its own account."""
        member = self._member_of_a()
        res = self.client.patch(
            "/scim/v2/Users/{0}".format(member["id"]), headers=self.b,
            json={"Operations": [{"op": "replace", "path": "active", "value": False}]})
        self.assertEqual(res.status_code, 404, res.text[:200])
        still = app_main.storage.get_member(self.a_tenant, member["id"])
        self.assertTrue(still.get("is_active", True),
                        "another tenant deactivated this workspace's member")

    def test_b_cannot_replace_as_member(self):
        member = self._member_of_a()
        res = self.client.put("/scim/v2/Users/{0}".format(member["id"]), headers=self.b,
                              json={"userName": "hijacked@tenant-b.example"})
        self.assertEqual(res.status_code, 404, res.text[:200])
        still = app_main.storage.get_member(self.a_tenant, member["id"])
        self.assertEqual(still["email"], "alice@tenant-a.example")

    def test_b_cannot_delete_as_member(self):
        member = self._member_of_a()
        self.client.delete("/scim/v2/Users/{0}".format(member["id"]), headers=self.b)
        self.assertIsNotNone(app_main.storage.get_member(self.a_tenant, member["id"]),
                             "another tenant removed this workspace's member")

    def test_scim_requires_authentication_at_all(self):
        """It is inside the middleware's prefix guard -- the comment there says leaving it out is
        the whole exploit, so assert the guard is actually in force."""
        self.assertEqual(self.client.get("/scim/v2/Users").status_code, 401)
        self.assertEqual(self.client.post("/scim/v2/Users", json={"userName": "x@y.z"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
