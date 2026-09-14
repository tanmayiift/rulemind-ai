"""Journeys as a first-class entity (parity P15).

THE ROW'S ESTIMATE WAS WRONG BY A LOT, and it is worth saying why once. It read: "No entity: a
journey cannot be created, locked, duplicated or run. It exists only as fixtures in three places
that do not share a definition, so there is not even one shape to build a table from. Largest item
in the matrix -- 3-6 months." There WAS one shape: `app/experience_studio.py` defines it,
`/sdk/v1/experience-manifest` serves it, and `packages/sdk-flutter/example/lib/state/app_state.dart`
reads `manifest['journeys']` and RUNS them against the named policy. So the fourth of the row's
four e2e clauses already worked, and the other three needed a table, not a re-architecture.

The e2eFlow is "create a journey -> lock it -> duplicate it -> run it from an SDK", and each
clause has a named test below. The interesting ones are about LOCK, because lock is the only new
idea here: it is not the dev/uat/prod lifecycle, and conflating the two would mean either that a
prod journey can be rewritten under live respondents or that a draft cannot be edited.
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
# Popped, not set to "none": AUTH_MODE=none disables RBAC, and an authorization suite that runs
# without auth proves nothing.
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import restore_storage_after  # noqa: E402

BASE = "/api/v1/journeys"

SCREENS = [
    {"id": "landing", "type": "landing", "title": "Apply in minutes"},
    {"id": "basics", "type": "form", "title": "About you",
     "fields": [{"id": "age", "label": "Age", "kind": "number"}]},
    {"id": "result", "type": "result", "title": "Decision"},
]


def body(**overrides):
    payload = {"title": "Personal Loan", "category": "Lending", "policy_id": "policy_x",
               "primary_color": "#496FBE", "screens": SCREENS}
    payload.update(overrides)
    return payload


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "j.db"))
        restore_storage_after(self)
        self.storage = app_main.storage
        self.client = TestClient(app_main.app)
        self.tenant = self.storage.default_tenant_id
        self.key = self.storage.default_api_key or ""
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        self.client.close()
        # Dispose the engine, not just the tempdir: db._engine_cache is keyed by URL and never
        # evicts, so the pool holds SQLite handles against a directory the next line deletes.
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        app_main.storage = self._orig
        restore_storage_after(self)
        self.tempdir.cleanup()

    def as_(self, key=None):
        return {"x-api-key": key or self.key}

    def key_for(self, role):
        return self.storage.generate_api_key_for_tenant(self.tenant, role=role)["plaintext"]

    def create(self, key=None, **overrides):
        response = self.client.post(BASE, headers=self.as_(key), json=body(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class TheE2eFlowTest(_Base):
    """create -> lock -> duplicate -> run from an SDK, one test per clause."""

    def test_create(self):
        journey = self.create()
        self.assertEqual(journey["title"], "Personal Loan")
        self.assertEqual(journey["step_count"], 3)
        self.assertEqual(journey["version"], 1)
        self.assertFalse(journey["is_locked"])

    def test_lock(self):
        journey = self.create()
        locked = self.client.post("{0}/{1}/lock".format(BASE, journey["id"]), headers=self.as_())
        self.assertEqual(locked.status_code, 200, locked.text)
        self.assertTrue(locked.json()["is_locked"])

    def test_duplicate(self):
        journey = self.create()
        self.client.post("{0}/{1}/lock".format(BASE, journey["id"]), headers=self.as_())
        copied = self.client.post("{0}/{1}/duplicate".format(BASE, journey["id"]),
                                  headers=self.as_(), json={})
        self.assertEqual(copied.status_code, 200, copied.text)
        copy_body = copied.json()
        self.assertNotEqual(copy_body["id"], journey["id"])
        self.assertEqual(copy_body["screens"], journey["screens"])
        self.assertEqual(copy_body["step_count"], 3)

    def test_run_it_from_an_sdk(self):
        """The clause that already worked before this entity existed -- the manifest endpoint and
        the Flutter example predate it. What is new is that the manifest now serves what THIS
        workspace authored instead of the same three demo journeys for everyone."""
        journey = self.create()
        manifest = self.client.get("/sdk/v1/experience-manifest", headers=self.as_())
        self.assertEqual(manifest.status_code, 200, manifest.text)
        journeys = manifest.json()["journeys"]
        self.assertEqual([j["id"] for j in journeys], [journey["id"]])
        # camelCase, because app_state.dart reads item['policyId'] and item['stepCount'].
        self.assertEqual(journeys[0]["policyId"], "policy_x")
        self.assertEqual(journeys[0]["stepCount"], 3)
        self.assertEqual(journeys[0]["screens"], SCREENS)


class TheDemoJourneysStillWorkTest(_Base):
    def test_a_workspace_that_has_authored_nothing_still_gets_the_demos(self):
        """The compatibility story. Before this entity the manifest was one static template, and
        the Flutter example, the QA suite and the screenshots all depend on those demo journeys
        being there for a fresh workspace."""
        manifest = self.client.get("/sdk/v1/experience-manifest", headers=self.as_()).json()
        self.assertTrue(manifest["journeys"], "a fresh workspace lost the demo journeys")
        self.assertIn("travel_guard", [j["id"] for j in manifest["journeys"]])

    def test_authored_journeys_replace_the_demos_rather_than_mixing_with_them(self):
        self.create()
        manifest = self.client.get("/sdk/v1/experience-manifest", headers=self.as_()).json()
        self.assertNotIn("travel_guard", [j["id"] for j in manifest["journeys"]])

    def test_demo_scenarios_go_when_a_workspace_has_its_own_journeys(self):
        """A scenario is a "try this case" button carrying a payload shaped for a DEMO journey's
        screens, and there is no API to author one.

        The first version filtered by surviving journeyId, which looks more careful and is worse.
        Journey ids are slugified titles, so a workspace that names a journey "Instant Personal
        Loan" gets `instant_personal_loan` -- the same id as a built-in demo -- and inherited five
        scenarios whose payloads answer screens its own journey does not have. That is what this
        test is really about, so it uses exactly that title.
        """
        self.create(title="Instant Personal Loan")
        manifest = self.client.get("/sdk/v1/experience-manifest", headers=self.as_()).json()
        self.assertEqual(manifest.get("scenarios"), [],
                         "demo scenarios survived onto a workspace's own journey")

    def test_a_fresh_workspace_keeps_the_demo_scenarios(self):
        """The control for the test above: dropping them unconditionally would gut the demo."""
        manifest = self.client.get("/sdk/v1/experience-manifest", headers=self.as_()).json()
        self.assertTrue(manifest.get("scenarios"), "a fresh workspace lost the demo scenarios")

    def test_an_inactive_journey_is_not_shipped_to_devices(self):
        """`is_active=False` is how an author retires a flow without deleting the record the
        decisions already made under it point at."""
        journey = self.create()
        self.client.put("{0}/{1}".format(BASE, journey["id"]), headers=self.as_(),
                        json={"is_active": False})
        manifest = self.client.get("/sdk/v1/experience-manifest", headers=self.as_()).json()
        self.assertNotIn(journey["id"], [j["id"] for j in manifest["journeys"]])


class LockingTest(_Base):
    """Lock is the only new idea here, so it gets the most tests."""

    def test_a_locked_journey_refuses_content_edits(self):
        journey = self.create()
        self.client.post("{0}/{1}/lock".format(BASE, journey["id"]), headers=self.as_())
        edited = self.client.put("{0}/{1}".format(BASE, journey["id"]), headers=self.as_(),
                                 json={"screens": SCREENS[:1]})
        self.assertEqual(edited.status_code, 409, edited.text)
        # The refusal has to say the way forward; "locked" with no exit is a dead end.
        self.assertIn("uplicate", edited.text)
        self.assertEqual(
            len(self.client.get("{0}/{1}".format(BASE, journey["id"]),
                                headers=self.as_()).json()["screens"]), 3)

    def test_a_locked_journey_can_still_be_promoted(self):
        """Lock is NOT the lifecycle. Conflating them would mean a journey cannot go to prod
        without first being editable under the people already filling it in."""
        journey = self.create()
        self.client.post("{0}/{1}/lock".format(BASE, journey["id"]), headers=self.as_())
        promoted = self.client.put("{0}/{1}".format(BASE, journey["id"]), headers=self.as_(),
                                   json={"status": "prod"})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(promoted.json()["status"], "prod")
        self.assertTrue(promoted.json()["is_locked"])

    def test_a_locked_journey_cannot_be_deleted(self):
        journey = self.create()
        self.client.post("{0}/{1}/lock".format(BASE, journey["id"]), headers=self.as_())
        deleted = self.client.delete("{0}/{1}".format(BASE, journey["id"]), headers=self.as_())
        self.assertEqual(deleted.status_code, 409, deleted.text)
        self.assertIsNotNone(self.storage.get_journey(journey["id"], tenant_id=self.tenant))

    def test_unlocking_restores_editing(self):
        journey = self.create()
        self.client.post("{0}/{1}/lock".format(BASE, journey["id"]), headers=self.as_())
        self.client.post("{0}/{1}/unlock".format(BASE, journey["id"]), headers=self.as_())
        edited = self.client.put("{0}/{1}".format(BASE, journey["id"]), headers=self.as_(),
                                 json={"screens": SCREENS[:1]})
        self.assertEqual(edited.status_code, 200, edited.text)

    def test_locking_does_not_bump_the_version(self):
        """The version answers "which screens did this respondent see". A version that moves when
        nothing a respondent sees has changed makes that question unanswerable, which is the only
        question the column exists for."""
        journey = self.create()
        locked = self.client.post("{0}/{1}/lock".format(BASE, journey["id"]),
                                  headers=self.as_()).json()
        self.assertEqual(locked["version"], journey["version"])

    def test_a_content_edit_does_bump_the_version(self):
        journey = self.create()
        edited = self.client.put("{0}/{1}".format(BASE, journey["id"]), headers=self.as_(),
                                 json={"screens": SCREENS[:2]}).json()
        self.assertEqual(edited["version"], journey["version"] + 1)


class DuplicationTest(_Base):
    def test_a_copy_arrives_unlocked_and_in_dev(self):
        """Duplication exists to make a LIVE flow changeable. A copy that arrived locked would
        defeat that outright, and one that arrived in prod would put an unreviewed journey in
        front of real people."""
        journey = self.create(status="prod")
        self.client.post("{0}/{1}/lock".format(BASE, journey["id"]), headers=self.as_())
        copied = self.client.post("{0}/{1}/duplicate".format(BASE, journey["id"]),
                                  headers=self.as_(), json={}).json()
        self.assertFalse(copied["is_locked"], "the copy inherited the lock it exists to escape")
        self.assertEqual(copied["status"], "dev", "an unreviewed copy landed in prod")

    def test_the_copy_is_independent_of_its_source(self):
        journey = self.create()
        copied = self.client.post("{0}/{1}/duplicate".format(BASE, journey["id"]),
                                  headers=self.as_(), json={}).json()
        self.client.put("{0}/{1}".format(BASE, copied["id"]), headers=self.as_(),
                        json={"screens": SCREENS[:1]})
        source = self.client.get("{0}/{1}".format(BASE, journey["id"]), headers=self.as_()).json()
        self.assertEqual(len(source["screens"]), 3, "editing the copy changed the original")

    def test_a_title_can_be_given_and_ids_do_not_collide(self):
        journey = self.create()
        first = self.client.post("{0}/{1}/duplicate".format(BASE, journey["id"]),
                                 headers=self.as_(), json={"title": "Personal Loan v2"}).json()
        second = self.client.post("{0}/{1}/duplicate".format(BASE, journey["id"]),
                                  headers=self.as_(), json={"title": "Personal Loan v2"}).json()
        self.assertEqual(first["title"], "Personal Loan v2")
        self.assertNotEqual(first["id"], second["id"])


class ValidationTest(_Base):
    def test_a_screen_without_an_id_is_refused(self):
        response = self.client.post(BASE, headers=self.as_(),
                                    json=body(screens=[{"type": "form"}]))
        self.assertEqual(response.status_code, 422, response.text)

    def test_duplicate_screen_ids_are_refused(self):
        """The SDK looks screens up by id, so two screens sharing one makes "which screen did
        they answer" ambiguous on the device."""
        response = self.client.post(BASE, headers=self.as_(), json=body(
            screens=[{"id": "a", "type": "form"}, {"id": "a", "type": "form"}]))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Duplicate", response.text)

    def test_the_screen_cap_is_enforced(self):
        """`screens` is a JSON column an authenticated author could otherwise grow without limit,
        and every device sync then carries it."""
        response = self.client.post(BASE, headers=self.as_(), json=body(
            screens=[{"id": "s{0}".format(i), "type": "form"} for i in range(61)]))
        self.assertEqual(response.status_code, 422, response.text)

    def test_an_unknown_journey_is_404_everywhere(self):
        for method, path in (("get", ""), ("put", ""), ("delete", ""),
                             ("post", "/lock"), ("post", "/unlock"), ("post", "/duplicate")):
            with self.subTest(method=method, path=path):
                call = getattr(self.client, method)
                url = "{0}/nope{1}".format(BASE, path)
                response = (call(url, headers=self.as_(), json={})
                            if method in ("put", "post") else call(url, headers=self.as_()))
                self.assertEqual(response.status_code, 404, response.text)


class RbacAcrossAllFiveRolesTest(_Base):
    ALL_ROLES = ("owner", "admin", "policy_maker", "reviewer", "viewer")
    AUTHORS = ("owner", "admin", "policy_maker")

    def test_the_capability_is_author_and_mapped_explicitly(self):
        from app import rbac

        self.assertIn("/api/v1/journeys", rbac._AUTHOR_PREFIXES)
        self.assertEqual(rbac.required_capability("POST", BASE), rbac.AUTHOR)

    def test_every_role_can_read(self):
        self.create()
        for role in self.ALL_ROLES:
            with self.subTest(role=role):
                key = self.key if role == "owner" else self.key_for(role)
                self.assertEqual(self.client.get(BASE, headers=self.as_(key)).status_code, 200)

    def test_only_author_roles_can_create(self):
        for role in self.ALL_ROLES:
            with self.subTest(role=role):
                key = self.key if role == "owner" else self.key_for(role)
                status = self.client.post(BASE, headers=self.as_(key), json=body()).status_code
                self.assertEqual(status, 200 if role in self.AUTHORS else 403,
                                 "{0} got {1} creating a journey".format(role, status))

    def test_a_viewer_cannot_lock_or_duplicate(self):
        journey = self.create()
        viewer = self.key_for("viewer")
        for path in ("lock", "unlock", "duplicate"):
            with self.subTest(path=path):
                response = self.client.post("{0}/{1}/{2}".format(BASE, journey["id"], path),
                                            headers=self.as_(viewer), json={})
                self.assertEqual(response.status_code, 403, response.text)


class TenantIsolationTest(_Base):
    def test_another_workspace_cannot_read_or_lock_this_journey(self):
        journey = self.create()
        other = self.storage.create_tenant("Other")
        other_key = self.storage.generate_api_key_for_tenant(other["id"], role="admin")["plaintext"]

        listed = self.client.get(BASE, headers=self.as_(other_key))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json(), [], "another workspace sees this workspace's journeys")

        self.assertEqual(self.client.get("{0}/{1}".format(BASE, journey["id"]),
                                         headers=self.as_(other_key)).status_code, 404)
        self.assertEqual(self.client.post("{0}/{1}/lock".format(BASE, journey["id"]),
                                          headers=self.as_(other_key), json={}).status_code, 404)

    def test_another_workspaces_manifest_does_not_carry_these_journeys(self):
        self.create()
        other = self.storage.create_tenant("Other")
        other_key = self.storage.generate_api_key_for_tenant(other["id"], role="admin")["plaintext"]
        manifest = self.client.get("/sdk/v1/experience-manifest",
                                   headers=self.as_(other_key)).json()
        self.assertNotIn("Personal Loan", [j["title"] for j in manifest["journeys"]])


class AuditTest(_Base):
    def _events(self, event_type):
        return [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                if e.get("event_type") == event_type]

    def test_creating_and_updating_are_audited(self):
        journey = self.create()
        self.assertTrue(self._events("journey_created"))
        self.client.put("{0}/{1}".format(BASE, journey["id"]), headers=self.as_(),
                        json={"title": "Renamed"})
        self.assertTrue(self._events("journey_updated"))

    def test_locking_is_audited_with_the_actor(self):
        """Storage's own `journey_created` carries only a version. Lock and duplicate are where
        WHO matters: a lock is someone declaring a flow live."""
        key = self.storage.generate_api_key_for_tenant(self.tenant, role="policy_maker")
        journey = self.create(key=key["plaintext"])
        self.client.post("{0}/{1}/lock".format(BASE, journey["id"]),
                         headers=self.as_(key["plaintext"]))
        events = self._events("journey_locked")
        self.assertEqual(len(events), 1)
        metadata = events[0].get("metadata") or {}
        self.assertEqual(metadata.get("actor_role"), "policy_maker")
        self.assertEqual(metadata.get("actor_api_key_id"), key["id"])

    def test_duplication_records_where_the_copy_came_from(self):
        """Without the source id, a second copy of a journey in the field has no provenance."""
        journey = self.create()
        copied = self.client.post("{0}/{1}/duplicate".format(BASE, journey["id"]),
                                  headers=self.as_(), json={}).json()
        events = self._events("journey_duplicated")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["entity_id"], copied["id"])
        self.assertEqual((events[0].get("metadata") or {}).get("source_journey_id"), journey["id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
