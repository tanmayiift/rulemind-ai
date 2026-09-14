"""Predictors end to end (parity P20): define one, decisions record values, the report shows drift.

That sentence is the row's e2eFlow, and `TheStatedFlowTest` at the bottom is that sentence. The
rest is the definition of done -- tenant isolation, RBAC across five roles, an audit event with
the actor, and negatives.

WHY THESE ROUTES ARE `author` AND WEBHOOK SUBSCRIPTIONS NEXT DOOR ARE `manage_access`: a
subscription sends this workspace's decision data to a THIRD PARTY and keeps doing so until
someone removes it; a predictor names a number that already exists and records it in this
workspace's own database. The first is standing egress configuration, the second is authoring in
the same class as a scorecard.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import predictors as predictors_module  # noqa: E402
from app import storage as storage_module  # noqa: E402
from app.models import PredictorValue  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import restore_storage_after  # noqa: E402

BASE = "/api/v1/predictors"


def body(**overrides):
    payload = {"name": "bureau score", "source_type": "variable", "source_id": "bureau_score"}
    payload.update(overrides)
    return payload


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._orig = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "pred.db"))
        restore_storage_after(self)
        self.storage = app_main.storage
        self.client = TestClient(app_main.app)
        self.tenant = self.storage.default_tenant_id
        self.owner_key = self.storage.default_api_key or ""
        storage_module.clear_decision_listeners()
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        storage_module.clear_decision_listeners()
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
        app_main.storage = self._orig
        restore_storage_after(self)
        self.tempdir.cleanup()

    def as_(self, key):
        return {"x-api-key": key}

    def key_for(self, role):
        return self.storage.generate_api_key_for_tenant(self.tenant, role=role)["plaintext"]

    def create(self, **overrides):
        response = self.client.post(BASE, headers=self.as_(self.owner_key), json=body(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def seed_values(self, predictor_id, values, days_ago_from, days_ago_to, tenant=None):
        """Write observations directly with a controlled created_at. The capture path is tested
        separately; this is for building a history a report can be run against without waiting
        thirty days."""
        tenant = tenant or self.tenant
        now = datetime.utcnow()
        span = max(1, days_ago_from - days_ago_to)
        with self.storage.connect() as session:
            for index, value in enumerate(values):
                offset = days_ago_from - (span * index / max(1, len(values)))
                session.add(PredictorValue(
                    tenant_id=tenant, predictor_id=predictor_id, value=float(value),
                    created_at=now - timedelta(days=offset)))


class CrudTest(_Base):

    def test_create_read_update_delete(self):
        created = self.create()
        self.assertEqual(created["source_type"], "variable")
        listed = self.client.get(BASE, headers=self.as_(self.owner_key)).json()
        self.assertEqual([p["id"] for p in listed], [created["id"]])

        patched = self.client.patch("{0}/{1}".format(BASE, created["id"]),
                                    headers=self.as_(self.owner_key), json={"is_active": False})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertFalse(patched.json()["is_active"])

        self.assertEqual(self.client.delete("{0}/{1}".format(BASE, created["id"]),
                                            headers=self.as_(self.owner_key)).status_code, 200)
        self.assertEqual(self.client.get(BASE, headers=self.as_(self.owner_key)).json(), [])

    def test_deleting_a_predictor_removes_its_observations(self):
        """Orphaned values would keep a deleted predictor's rows growing with nothing able to
        read them."""
        created = self.create()
        self.seed_values(created["id"], list(range(100)), 20, 10)
        self.client.delete("{0}/{1}".format(BASE, created["id"]), headers=self.as_(self.owner_key))
        with self.storage.connect() as session:
            remaining = session.query(PredictorValue).filter(
                PredictorValue.predictor_id == created["id"]).count()
        self.assertEqual(remaining, 0)

    def test_the_source_types_are_served_rather_than_hardcoded_in_the_ui(self):
        response = self.client.get("{0}/source-types".format(BASE), headers=self.as_(self.owner_key))
        self.assertEqual(response.json()["source_types"], list(predictors_module.SOURCE_TYPES))


class NegativeTest(_Base):

    def test_an_unknown_source_type_is_refused(self):
        response = self.client.post(BASE, headers=self.as_(self.owner_key),
                                    json=body(source_type="vibes"))
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_variable_predictor_without_a_source_id_is_refused(self):
        """Without it there is nothing to read from a decision, so the row would record nothing
        forever and look configured."""
        response = self.client.post(BASE, headers=self.as_(self.owner_key),
                                    json={"name": "x", "source_type": "variable"})
        self.assertEqual(response.status_code, 422)

    def test_an_outcome_predictor_needs_no_source_id(self):
        self.assertEqual(
            self.client.post(BASE, headers=self.as_(self.owner_key),
                             json={"name": "approval rate", "source_type": "outcome"}).status_code,
            200)

    def test_absurd_windows_are_refused(self):
        for patch in ({"baseline_days": 0}, {"baseline_days": 100000}, {"bins": 0}, {"bins": 999}):
            with self.subTest(patch=patch):
                self.assertEqual(
                    self.client.post(BASE, headers=self.as_(self.owner_key),
                                     json=body(**patch)).status_code, 422)

    def test_a_missing_predictor_is_404_everywhere(self):
        for call in (lambda: self.client.get("{0}/nope".format(BASE), headers=self.as_(self.owner_key)),
                     lambda: self.client.delete("{0}/nope".format(BASE), headers=self.as_(self.owner_key)),
                     lambda: self.client.get("{0}/nope/drift".format(BASE), headers=self.as_(self.owner_key))):
            self.assertEqual(call().status_code, 404)

    def test_an_unauthenticated_caller_is_rejected(self):
        self.assertEqual(self.client.get(BASE).status_code, 401)


class RbacTest(_Base):

    def test_every_role_can_read(self):
        self.create()
        for role in ("owner", "admin", "policy_maker", "reviewer", "viewer"):
            with self.subTest(role=role):
                key = self.owner_key if role == "owner" else self.key_for(role)
                self.assertEqual(self.client.get(BASE, headers=self.as_(key)).status_code, 200)

    def test_authoring_roles_can_create_and_others_cannot(self):
        for role, expected in (("owner", 200), ("admin", 200), ("policy_maker", 200),
                               ("reviewer", 403), ("viewer", 403)):
            with self.subTest(role=role):
                key = self.owner_key if role == "owner" else self.key_for(role)
                status = self.client.post(BASE, headers=self.as_(key),
                                          json=body(name="p-{0}".format(role))).status_code
                self.assertEqual(status, expected, "{0} got {1}".format(role, status))

    def test_a_policy_maker_can_author_one_which_is_the_point_of_author(self):
        """The fail-closed default is manage_access, so an unlisted prefix would lock the very
        role this entity is for out of it."""
        from app import rbac

        self.assertEqual(rbac.required_capability("POST", "/api/v1/predictors"), rbac.AUTHOR)


class TenantIsolationTest(_Base):

    def setUp(self):
        super().setUp()
        self.other = self.storage.create_tenant("Other Co")["id"]
        self.other_key = self.storage.generate_api_key_for_tenant(
            self.other, role="owner")["plaintext"]

    def test_another_workspace_cannot_reach_it(self):
        created = self.create()
        path = "{0}/{1}".format(BASE, created["id"])
        for call in (lambda: self.client.get(path, headers=self.as_(self.other_key)),
                     lambda: self.client.delete(path, headers=self.as_(self.other_key)),
                     lambda: self.client.get(path + "/drift", headers=self.as_(self.other_key))):
            self.assertEqual(call().status_code, 404)
        self.assertEqual(self.client.get(BASE, headers=self.as_(self.other_key)).json(), [])

    def test_another_workspaces_observations_never_enter_the_report(self):
        """The isolation that matters most here: the values table is queried in bulk, so a
        missing tenant predicate would silently blend two workspaces' distributions into one
        drift number."""
        mine = self.create()
        self.seed_values(mine["id"], [700.0] * 200, 40, 8)
        self.seed_values(mine["id"], [700.0] * 200, 6, 0)
        # Same predictor id, different tenant, wildly different values.
        self.seed_values(mine["id"], [100.0] * 500, 6, 0, tenant=self.other)
        report = self.client.get("{0}/{1}/drift".format(BASE, mine["id"]),
                                 headers=self.as_(self.owner_key)).json()
        self.assertEqual(report["current_count"], 200,
                         "another workspace's observations entered the report")
        self.assertEqual(report["classification"], "stable")


class AuditTest(_Base):

    def test_mutations_are_audited_with_the_actor(self):
        created = self.create()
        self.client.delete("{0}/{1}".format(BASE, created["id"]), headers=self.as_(self.owner_key))
        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e.get("entity_type") == "predictor"]
        types = {e["event_type"] for e in events}
        self.assertIn("predictor_created", types)
        self.assertIn("predictor_deleted", types)
        metadata = events[0].get("metadata") or events[0].get("metadata_json") or {}
        self.assertTrue(metadata.get("actor_api_key_id"), "the audit event has no actor")


class ValueCaptureTest(_Base):
    """The middle third of the flow: a decision writes an observation."""

    def setUp(self):
        super().setUp()
        storage_module.register_decision_listener(predictors_module.on_decision)

    def decide(self, **overrides):
        payload = {"id": "dec_{0}".format(len(overrides)), "tenant_id": self.tenant,
                   "policy_id": "pol", "outcome": "approve", "payload": {},
                   "computed_variables": {"bureau_score": 712},
                   "source": "api"}
        payload.update(overrides)
        return self.storage.add_decision(payload, tenant_id=self.tenant)

    def values_for(self, predictor_id):
        return self.storage.predictor_values_between(
            self.tenant, predictor_id, datetime.utcnow() - timedelta(days=1),
            datetime.utcnow() + timedelta(days=1))

    def test_a_decision_records_a_value(self):
        created = self.create()
        self.decide()
        self.assertEqual(self.values_for(created["id"]), [712.0])

    def test_an_inactive_predictor_records_nothing(self):
        created = self.create(is_active=False)
        self.decide()
        self.assertEqual(self.values_for(created["id"]), [])

    def test_a_decision_missing_the_variable_records_a_gap_not_a_zero(self):
        """A zero would drag the distribution toward a value nothing produced -- drift that is
        really a computation failure."""
        created = self.create()
        self.decide(computed_variables={"something_else": 1})
        self.assertEqual(self.values_for(created["id"]), [])

    def test_a_boolean_variable_is_not_recorded_as_one_point_zero(self):
        """isinstance(True, int) is True in Python. A flag binned as a score produces a chart
        that looks like data."""
        created = self.create()
        self.decide(computed_variables={"bureau_score": True})
        self.assertEqual(self.values_for(created["id"]), [])

    def test_an_outcome_predictor_records_the_outcome_ordinally(self):
        created = self.create(name="approval", source_type="outcome", source_id=None)
        self.decide(outcome="reject")
        self.assertEqual(self.values_for(created["id"]), [0.0])

    def test_a_failing_predictor_never_fails_the_decision(self):
        """An observer must not be able to break the thing it observes."""
        self.create()
        original = type(self.storage).record_predictor_values

        def exploding(self_inner, tenant_id, rows):
            raise RuntimeError("values table is on fire")

        type(self.storage).record_predictor_values = exploding
        try:
            self.decide()
        finally:
            type(self.storage).record_predictor_values = original
        self.assertEqual(len(self.storage.list_decisions(tenant_id=self.tenant)), 1)

    def test_on_decision_swallows_its_own_failures(self):
        """The guard INSIDE predictors.on_decision, reached directly.

        storage._notify_decision_listeners also wraps every listener, so the test above passes
        with the inner try/except deleted -- a mutation proved it. Two layers is correct
        (defence in depth), but only one of them was covered, and the inner one is the layer that
        keeps a broken predictor from taking the OTHER listeners down with it, since a raise
        there would abort this listener before webhooks ever ran."""
        self.create()
        original = type(self.storage).record_predictor_values

        def exploding(self_inner, tenant_id, rows):
            raise RuntimeError("values table is on fire")

        type(self.storage).record_predictor_values = exploding
        try:
            # Must not raise. Called directly, so storage's own wrapper is not in the way.
            predictors_module.on_decision(
                self.storage,
                {"id": "d1", "tenant_id": self.tenant, "outcome": "approve",
                 "computed_variables": {"bureau_score": 700}})
        finally:
            type(self.storage).record_predictor_values = original

    def test_a_predictor_lookup_failure_is_also_swallowed(self):
        original = type(self.storage).active_predictors

        def exploding(self_inner, tenant_id):
            raise RuntimeError("cannot read predictors")

        type(self.storage).active_predictors = exploding
        try:
            predictors_module.on_decision(
                self.storage, {"id": "d1", "tenant_id": self.tenant, "outcome": "approve"})
        finally:
            type(self.storage).active_predictors = original


class ColdStoreFanOutTest(_Base):
    """Predictor values reach the configured OLAP sink as well as the primary DB (P10 + P20).

    A FAN-OUT, not a move: a decision is archived when it falls OUT of the retention window and
    is then purged, while a predictor value is copied out as it is produced and STAYS -- the hot
    copy is what the drift report reads. The cold copy is for the long-horizon analysis a primary
    database is the wrong shape for.
    """

    def setUp(self):
        super().setUp()
        storage_module.register_decision_listener(predictors_module.on_decision)
        from app.archiver import MemoryArchiver

        self.sink = MemoryArchiver()

    def decide(self, **overrides):
        payload = {"id": "dec_cold", "tenant_id": self.tenant, "policy_id": "pol",
                   "outcome": "approve", "payload": {},
                   "computed_variables": {"bureau_score": 712}, "source": "api"}
        payload.update(overrides)
        return self.storage.add_decision(payload, tenant_id=self.tenant)

    def decide_with_sink(self, enabled=True, sink=None, **overrides):
        from unittest import mock as _mock

        with _mock.patch("app.archiver.archiving_enabled", return_value=enabled):
            with _mock.patch("app.archiver.get_archiver", return_value=sink or self.sink):
                return self.decide(**overrides)

    def hot_values(self, predictor_id):
        return self.storage.predictor_values_between(
            self.tenant, predictor_id, datetime.utcnow() - timedelta(days=1),
            datetime.utcnow() + timedelta(days=1))

    def test_a_value_reaches_both_the_hot_db_and_the_sink(self):
        created = self.create()
        self.decide_with_sink()
        self.assertEqual(self.hot_values(created["id"]), [712.0], "the hot copy is missing")
        self.assertEqual(len(self.sink.predictor_values), 1, "the cold copy is missing")

    def test_the_archived_row_carries_its_tenant_and_predictor(self):
        """A cold store row with no workspace or predictor on it cannot be queried for anything."""
        created = self.create()
        self.decide_with_sink()
        row = self.sink.predictor_values[0]
        self.assertEqual(row["tenant_id"], self.tenant)
        self.assertEqual(row["predictor_id"], created["id"])
        self.assertEqual(row["value"], 712.0)
        self.assertTrue(row["created_at"], "no timestamp, so it cannot be windowed")

    def test_the_hot_copy_is_kept_not_moved(self):
        """The drift report reads the hot copy. Treating this like decision archiving -- write
        then purge -- would empty the table the report queries."""
        created = self.create()
        self.decide_with_sink()
        self.assertEqual(len(self.hot_values(created["id"])), 1)

    def test_no_sink_configured_is_a_silent_no_op(self):
        created = self.create()
        self.decide_with_sink(enabled=False)
        self.assertEqual(self.hot_values(created["id"]), [712.0])
        self.assertEqual(self.sink.predictor_values, [])

    def test_a_broken_sink_never_costs_the_decision_or_the_hot_copy(self):
        """The sink's client is a lazy optional import that is NOT in the shipped image, and this
        runs on the decision path."""
        class _Exploding:
            name = "exploding"

            def write(self_inner, decisions):
                raise RuntimeError("sink down")

            def write_predictor_values(self_inner, values):
                raise RuntimeError("sink down")

        created = self.create()
        self.decide_with_sink(sink=_Exploding())
        self.assertEqual(len(self.storage.list_decisions(tenant_id=self.tenant)), 1,
                         "a dead cold store lost the decision")
        self.assertEqual(self.hot_values(created["id"]), [712.0],
                         "a dead cold store lost the hot copy")

    def test_the_fan_out_swallows_its_own_failures(self):
        """The guard INSIDE _fan_out_to_archive, reached directly.

        storage._notify_decision_listeners wraps every listener, so the decision-level test above
        passes with this try/except deleted -- a mutation proved it, and it is the THIRD time in
        this codebase that an outer defence-in-depth wrapper has masked an inner guard. Two layers
        is correct; testing only the outer one means the inner one is decoration. Here the inner
        one is what keeps a dead cold store from aborting on_decision before the HOT write's own
        error handling is reached.
        """
        class _Exploding:
            name = "exploding"

            def write(self_inner, decisions):
                raise RuntimeError("sink down")

            def write_predictor_values(self_inner, values):
                raise RuntimeError("sink down")

        from unittest import mock as _mock

        with _mock.patch("app.archiver.archiving_enabled", return_value=True):
            with _mock.patch("app.archiver.get_archiver", return_value=_Exploding()):
                # Must not raise.
                predictors_module._fan_out_to_archive(self.tenant, [{"predictor_id": "p", "value": 1.0}])

    def test_an_unimportable_sink_client_is_swallowed_too(self):
        """The realistic failure: clickhouse-connect is a lazy optional import and is NOT in the
        shipped image, so a deployment that sets DECISION_ARCHIVE_SINK=clickhouse without it
        raises ImportError on the first call -- on the decision path."""
        from unittest import mock as _mock

        with _mock.patch("app.archiver.archiving_enabled", return_value=True):
            with _mock.patch("app.archiver.get_archiver",
                             side_effect=ImportError("No module named 'clickhouse_connect'")):
                predictors_module._fan_out_to_archive(self.tenant, [{"predictor_id": "p", "value": 1.0}])

    def test_a_sink_without_the_method_is_fine(self):
        """write_predictor_values is a DEFAULTED no-op on the interface, so an out-of-tree sink
        that predates it keeps working."""
        from app.archiver import NullArchiver

        created = self.create()
        self.decide_with_sink(sink=NullArchiver())
        self.assertEqual(self.hot_values(created["id"]), [712.0])


class TheStatedFlowTest(_Base):
    """P20's e2eFlow: define a predictor -> values accumulate -> the report shows drift."""

    def test_a_stable_population_reports_stable(self):
        created = self.create()
        self.seed_values(created["id"], [700 + (i % 40) for i in range(400)], 35, 8)
        self.seed_values(created["id"], [700 + (i % 40) for i in range(200)], 6, 0)
        report = self.client.get("{0}/{1}/drift".format(BASE, created["id"]),
                                 headers=self.as_(self.owner_key)).json()
        self.assertEqual(report["classification"], "stable", report)

    def test_a_shifted_population_reports_significant_drift(self):
        created = self.create()
        self.seed_values(created["id"], [700 + (i % 40) for i in range(400)], 35, 8)
        self.seed_values(created["id"], [560 + (i % 40) for i in range(200)], 6, 0)
        report = self.client.get("{0}/{1}/drift".format(BASE, created["id"]),
                                 headers=self.as_(self.owner_key)).json()
        self.assertEqual(report["classification"], "significant", report)
        self.assertGreater(report["psi"], 0.25)
        self.assertTrue(report["bins"], "the report says drift but not where")

    def test_no_history_is_insufficient_data_not_stable(self):
        created = self.create()
        report = self.client.get("{0}/{1}/drift".format(BASE, created["id"]),
                                 headers=self.as_(self.owner_key)).json()
        self.assertIsNone(report["psi"])
        self.assertEqual(report["classification"], "insufficient_data")

    def test_the_windows_are_adjacent_not_overlapping(self):
        """An overlapping baseline contains the very observations it is compared against, which
        damps exactly the signal the report exists to show.

        Asserted on what the QUERY returns, not on the reported boundaries: the response computes
        both windows from the same `current_start`, so the reported pair stays adjacent even if
        the baseline QUERY is widened to overlap -- a mutation proved a boundary-only assertion
        passes against exactly that bug. Seeding only the current window and requiring an EMPTY
        baseline is what actually distinguishes them.
        """
        created = self.create()
        self.seed_values(created["id"], [700.0] * 300, 5, 0)
        report = self.client.get("{0}/{1}/drift".format(BASE, created["id"]),
                                 headers=self.as_(self.owner_key)).json()
        self.assertEqual(report["baseline_count"], 0,
                         "the baseline window reached into the current window: {0} values".format(
                             report["baseline_count"]))
        self.assertEqual(report["current_count"], 300)
        self.assertEqual(report["baseline_window"][1], report["current_window"][0])

    def test_the_response_echoes_the_windows_it_used(self):
        """So a screenshot of a drift number is self-describing."""
        created = self.create(baseline_days=60, current_days=14)
        report = self.client.get("{0}/{1}/drift".format(BASE, created["id"]),
                                 headers=self.as_(self.owner_key)).json()
        self.assertEqual((report["baseline_days"], report["current_days"]), (60, 14))

    def test_windows_can_be_overridden_per_request_for_exploration(self):
        created = self.create()
        report = self.client.get("{0}/{1}/drift?baseline_days=90&current_days=30".format(
            BASE, created["id"]), headers=self.as_(self.owner_key)).json()
        self.assertEqual((report["baseline_days"], report["current_days"]), (90, 30))

    def test_an_absurd_override_is_refused(self):
        created = self.create()
        self.assertEqual(self.client.get(
            "{0}/{1}/drift?baseline_days=99999".format(BASE, created["id"]),
            headers=self.as_(self.owner_key)).status_code, 422)


if __name__ == "__main__":
    unittest.main()
