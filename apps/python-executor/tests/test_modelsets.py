"""Model sets: group, pin, route, compare (parity P05).

Models were flat blobs addressed one at a time -- a `model` step names one public_id and that is
the whole story. There was no way to say "use the thin-file model when there is no bureau history
and the main model otherwise", and no way to compare two candidates on the same input without
wiring two policies and diffing decision logs.

THE PIN IS THE REASON IT IS CALLED A VERSIONED SET. `hosted_models` is unique on
(tenant, public_id) and carries `version` as a COLUMN, so a retrained model replaces its row in
place and every reference silently follows. Fine for a single model step, where the author is
looking at the model they named. Not fine for a set whose purpose is a controlled comparison: a
member that moved underneath it makes yesterday's routing decision and today's incomparable, and
nothing would say so. The pin does not FREEZE anything -- the old blob is gone -- it records what
was expected so the drift is visible.
"""
from __future__ import annotations

import base64
import os
import pickle
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
from app import modelsets  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import restore_storage_after  # noqa: E402

BASE = "/api/v1/model-sets"


class _Constant:
    """A model that always answers the same number, so a comparison's output is checkable."""

    def __init__(self, value=0):
        self.value = value

    def predict(self, rows):
        return [self.value for _ in rows]

    def predict_proba(self, rows):
        return [[0.5, 0.5] for _ in rows]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "sets.db"))
        restore_storage_after(self)
        self.storage = app_main.storage
        self.client = TestClient(app_main.app)
        self.tenant = self.storage.default_tenant_id
        self.key = self.storage.default_api_key or ""
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        self.client.close()
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        app_main.storage = self._orig
        restore_storage_after(self)
        self.tempdir.cleanup()

    def as_(self, key=None):
        return {"x-api-key": key or self.key}

    def upload_model(self, public_id, value=0):
        response = self.client.post("/api/v1/models", headers=self.as_(), json={
            "id": public_id, "name": public_id, "model_type": "sklearn",
            "model_base64": base64.b64encode(pickle.dumps(_Constant(value))).decode()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create_set(self, members=None, **overrides):
        payload = {"id": "set_a", "name": "candidates",
                   "members": members if members is not None else [
                       {"model_id": "m_thin", "label": "thin",
                        "condition": "variables.get('bureau_hits', 0) == 0"},
                       {"model_id": "m_main", "label": "main"}]}
        payload.update(overrides)
        return self.client.post(BASE, headers=self.as_(), json=payload)


class ValidationTest(_Base):
    """Refused at authoring time rather than stored as a set that routes to nothing."""

    def setUp(self):
        super().setUp()
        self.upload_model("m_main", 1)
        self.upload_model("m_thin", 2)

    def test_a_valid_set_is_created_and_pinned(self):
        created = self.create_set().json()
        self.assertEqual([m["label"] for m in created["members"]], ["thin", "main"])
        self.assertTrue(all(m["pinned_version"] for m in created["members"]),
                        "members were stored without a version pin")

    def test_a_member_naming_an_unknown_model_is_refused(self):
        response = self.create_set(members=[{"model_id": "m_ghost", "label": "ghost"}])
        self.assertEqual(response.status_code, 422)
        self.assertIn("m_ghost", response.text)

    def test_an_empty_member_list_is_refused(self):
        self.assertEqual(self.create_set(members=[]).status_code, 422)

    def test_duplicate_labels_are_refused(self):
        """Labels key the comparison output; duplicates would silently overwrite each other and
        the operator would see fewer candidates than they configured."""
        response = self.create_set(members=[
            {"model_id": "m_main", "label": "same"}, {"model_id": "m_thin", "label": "same"}])
        self.assertEqual(response.status_code, 422)
        self.assertIn("duplicate", response.text.lower())

    def test_a_non_string_condition_is_refused(self):
        response = self.create_set(members=[
            {"model_id": "m_main", "label": "a", "condition": {"not": "a string"}}])
        self.assertEqual(response.status_code, 422)

    def test_too_many_members_are_refused(self):
        self.assertEqual(self.create_set(members=[
            {"model_id": "m_main", "label": "m{0}".format(i)}
            for i in range(modelsets.MAX_MEMBERS + 1)]).status_code, 422)


class RoutingTest(_Base):
    """First match wins, exactly as a branch step's branches are evaluated."""

    def route(self, members, view):
        return modelsets.resolve_member({"id": "s", "members": members}, view)

    def test_the_first_matching_condition_wins(self):
        member, _ = self.route([
            {"model_id": "a", "label": "a", "condition": "score > 700"},
            {"model_id": "b", "label": "b", "condition": "score > 500"},
        ], {"score": 800})
        self.assertEqual(member["label"], "a")

    def test_order_is_the_authors_and_is_respected(self):
        """Swap the two and the same input routes elsewhere -- the list is the semantics."""
        member, _ = self.route([
            {"model_id": "b", "label": "b", "condition": "score > 500"},
            {"model_id": "a", "label": "a", "condition": "score > 700"},
        ], {"score": 800})
        self.assertEqual(member["label"], "b")

    def test_a_member_with_no_condition_is_the_default(self):
        member, reason = self.route([
            {"model_id": "a", "label": "a", "condition": "score > 900"},
            {"model_id": "b", "label": "fallback"},
        ], {"score": 100})
        self.assertEqual(member["label"], "fallback")
        self.assertIn("unconditional", reason)

    def test_no_match_and_no_default_routes_nowhere(self):
        member, reason = self.route([
            {"model_id": "a", "label": "a", "condition": "score > 900"}], {"score": 100})
        self.assertIsNone(member)
        self.assertIn("no member matched", reason)

    def test_an_unevaluable_condition_is_skipped_not_matched(self):
        """Falling through to the default is a defined outcome the author chose; a malformed
        condition silently selecting a model is not."""
        member, _ = self.route([
            {"model_id": "a", "label": "broken", "condition": "this is not an expression ("},
            {"model_id": "b", "label": "fallback"},
        ], {"score": 100})
        self.assertEqual(member["label"], "fallback")

    def test_a_missing_field_does_not_select_a_model(self):
        member, _ = self.route([
            {"model_id": "a", "label": "a", "condition": "absent_field > 5"},
            {"model_id": "b", "label": "fallback"},
        ], {})
        self.assertEqual(member["label"], "fallback")


class DriftTest(_Base):
    def setUp(self):
        super().setUp()
        self.upload_model("m_main", 1)
        self.upload_model("m_thin", 2)
        self.set_id = self.create_set().json()["id"]

    def test_a_fresh_set_is_in_sync(self):
        body = self.client.get("{0}/{1}/drift".format(BASE, self.set_id), headers=self.as_()).json()
        self.assertTrue(body["in_sync"])
        self.assertEqual(body["drifted"], [])

    def test_retraining_a_member_shows_as_drift(self):
        """The pin is on the model's BYTES, not on its `version` column.

        routers/models.py writes `"version": 1` on every upload and nothing increments it, so a
        retrained model keeps version 1 and a version pin would report "in sync" against different
        weights. The first version of this test SKIPPED for exactly that reason -- and a skip is
        not a pass. A feature whose premise rests on a column nobody maintains quietly does
        nothing, so drift is decided on a content hash.
        """
        # Retraining, as the API actually allows it: there is NO update endpoint for models --
        # only create (409 on a collision), get and delete -- so replacing a model means delete
        # then re-create under the same public_id. The set's member still names that id, and the
        # blob differs, which is exactly what the fingerprint is for.
        self.client.delete("/api/v1/models/m_main", headers=self.as_())
        self.upload_model("m_main", 9)
        body = self.client.get("{0}/{1}/drift".format(BASE, self.set_id), headers=self.as_()).json()
        self.assertFalse(body["in_sync"], "retraining a member was not detected")
        drifted = [d for d in body["drifted"] if d["label"] == "main"]
        self.assertEqual(len(drifted), 1)
        self.assertIn("weights changed", drifted[0]["reason"])

    def test_an_unchanged_member_is_not_reported_as_drifted(self):
        """The control: re-uploading the SAME model must not read as a retrain, or the report
        cries wolf on every deploy that touches nothing."""
        self.client.delete("/api/v1/models/m_thin", headers=self.as_())
        self.upload_model("m_thin", 2)  # same weights -> byte-identical blob
        body = self.client.get("{0}/{1}/drift".format(BASE, self.set_id), headers=self.as_()).json()
        self.assertEqual([d["label"] for d in body["drifted"]], [])

    def test_the_fingerprint_is_recorded_at_pin_time(self):
        stored = self.client.get("{0}/{1}".format(BASE, self.set_id), headers=self.as_()).json()
        self.assertTrue(all(m.get("pinned_fingerprint") for m in stored["members"]),
                        "members were pinned without a content fingerprint")

    def test_a_deleted_member_shows_as_drift_rather_than_a_crash(self):
        self.client.delete("/api/v1/models/m_thin", headers=self.as_())
        body = self.client.get("{0}/{1}/drift".format(BASE, self.set_id), headers=self.as_()).json()
        self.assertFalse(body["in_sync"])
        self.assertIn("no longer exists", body["drifted"][0]["reason"])

    def test_drift_is_reported_not_enforced(self):
        """Retraining is legitimate; blocking a decision over it would make the feature a
        liability. The set owes the operator VISIBILITY, not prevention."""
        self.client.delete("/api/v1/models/m_thin", headers=self.as_())
        # The set is still readable and still routes; only the report changes.
        self.assertEqual(self.client.get("{0}/{1}".format(BASE, self.set_id),
                                         headers=self.as_()).status_code, 200)


class CompareTest(_Base):
    def setUp(self):
        super().setUp()
        self.upload_model("m_main", 1)
        self.upload_model("m_thin", 2)
        self.set_id = self.create_set().json()["id"]

    def compare(self, features=None):
        return self.client.post("{0}/{1}/compare".format(BASE, self.set_id),
                                headers=self.as_(), json={"features": features or {}})

    def test_every_member_runs_on_the_same_input(self):
        body = self.compare({"bureau_hits": 3}).json()
        self.assertEqual(sorted(r["label"] for r in body["results"]), ["main", "thin"])

    def test_routing_is_bypassed_but_still_reported(self):
        """The point of a comparison is to see what each candidate WOULD have said, including the
        one this input would not have routed to -- while still saying which routing picked."""
        body = self.compare({"bureau_hits": 3}).json()
        self.assertEqual(len(body["results"]), 2, "compare only ran the routed member")
        self.assertIn("routed_to", body)

    def test_a_failing_member_is_a_result_not_an_outage(self):
        """A candidate that errors is exactly what an operator is looking for; hiding the others
        behind it would defeat the endpoint."""
        self.client.delete("/api/v1/models/m_thin", headers=self.as_())
        body = self.compare().json()
        self.assertEqual(len(body["results"]), 2)
        errored = [r for r in body["results"] if r.get("error")]
        self.assertEqual(len(errored), 1)
        self.assertTrue([r for r in body["results"] if r.get("result")],
                        "one bad member hid the working one")

    def test_the_pinned_and_current_versions_are_both_reported(self):
        body = self.compare().json()
        working = [r for r in body["results"] if not r.get("error")][0]
        self.assertIn("pinned_version", working)
        self.assertIn("current_version", working)


class TenantIsolationTest(_Base):
    def setUp(self):
        super().setUp()
        self.upload_model("m_main", 1)
        self.upload_model("m_thin", 2)
        self.set_id = self.create_set().json()["id"]
        self.other = self.storage.create_tenant("Other Co")["id"]
        self.other_key = self.storage.generate_api_key_for_tenant(
            self.other, role="owner")["plaintext"]

    def test_another_workspace_cannot_reach_it(self):
        for path in ("", "/drift"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(
                    "{0}/{1}{2}".format(BASE, self.set_id, path),
                    headers=self.as_(self.other_key)).status_code, 404)
        self.assertEqual(self.client.post("{0}/{1}/compare".format(BASE, self.set_id),
                                          headers=self.as_(self.other_key), json={}).status_code, 404)
        self.assertEqual(self.client.get(BASE, headers=self.as_(self.other_key)).json(), [])


class RbacTest(_Base):
    def test_authoring_roles_can_create_and_others_cannot(self):
        self.upload_model("m_main", 1)
        self.upload_model("m_thin", 2)
        for role, expected in (("admin", 200), ("policy_maker", 200),
                               ("reviewer", 403), ("viewer", 403)):
            with self.subTest(role=role):
                key = self.storage.generate_api_key_for_tenant(
                    self.tenant, role=role)["plaintext"]
                status = self.client.post(BASE, headers=self.as_(key), json={
                    "id": "set_{0}".format(role), "name": role,
                    "members": [{"model_id": "m_main", "label": "only"}]}).status_code
                self.assertEqual(status, expected, "{0} got {1}".format(role, status))


if __name__ == "__main__":
    unittest.main()
