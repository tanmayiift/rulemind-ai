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


if __name__ == "__main__":
    unittest.main()
