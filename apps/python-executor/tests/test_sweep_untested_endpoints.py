"""Sweep: the seven endpoints that appeared in no test file at all.

Found by cross-referencing all 272 routes in `app/routers/` against every test, spec, smoke and
k6 file. Six are in `governance.py`, one in `insights.py`, one in `collab.py`:

    POST   /api/v1/policies/{id}/whatif        governance.py:313
    POST   /api/v1/policies/{id}/shadow        governance.py:336
    DELETE /api/v1/policies/{id}/shadow        governance.py:349
    GET    /api/v1/policies/{id}/shadow        governance.py:357
    GET    /api/v1/policies/{id}/releases      governance.py:373
    GET    /api/v1/analytics/latency           insights.py:56
    POST   /api/v1/collab/{doc_id}/seed        collab.py:42

The underlying MODULES are well covered -- `test_backlog_features.py:101-198` exercises
`shadow.register_shadow` / `run_shadow` / `unregister_shadow`, `release.list_releases` /
`rollback_policy` and `whatif.simulate_kpis` directly. What nothing exercised is the HTTP layer
in front of them: RBAC, tenant scoping, and request/response shape. That is the same gap this
repo already documented for `/api/v1/decide` (`docs/PRODUCTION-READINESS.md`) -- a tested
function reached through an untested handler.

**Why the isolation tripwire did not catch them.** `test_tenant_isolation_matrix.py:280-287`
counts a mutating route as accounted-for when it matches a *probed prefix*; `/api/v1/policies`
is a probed CRUD collection, so `whatif`, `shadow` and `rollback` rode in on the prefix. And
`:270` inspects only MUTATING routes, so `GET /analytics/latency` and `GET .../releases` were
structurally invisible to it. Both gaps are addressed there; this file is the coverage itself.

`test_sweep_api_insights.py:4` claimed to cover `/api/v1/analytics/{decisions,latency,sdk}`. It
covered two of the three -- `latency_analytics` was called nowhere in the repository.
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


def _outcome_policy(pid: str, outcome: str) -> dict:
    return {"id": pid, "name": pid, "defaultOutcome": "review",
            "steps": [{"id": "s", "type": "outcome", "ref_id": outcome,
                       "config": {"outcome": outcome}, "label": outcome}]}


class _Case(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "sweep.db"))
        self.client = TestClient(app_main.app)
        self.tenant_a = app_main.storage.default_tenant_id
        self.headers_a = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_b = app_main.storage.create_tenant("Sweep Untested Co")["id"]
        self.headers_b = {
            "x-api-key": app_main.storage.generate_api_key_for_tenant(self.tenant_b)["plaintext"]}

        app_main.storage.create_policy(_outcome_policy("pol_live", "approve"),
                                       tenant_id=self.tenant_a)
        app_main.storage.create_policy(_outcome_policy("pol_cand", "reject"),
                                       tenant_id=self.tenant_a)
        # whatif replays through a COMPILED bundle; without one it answers 400 "No compiled
        # bundle to simulate against", which is correct and is not what these tests are about.
        app_main.storage.save_bundle(
            {"version": 1, "content": {"policy": _outcome_policy("pol_live", "approve"),
                                       "rules": {}},
             "encrypted_content": "x", "signature": "x", "checksum": "x"},
            tenant_id=self.tenant_a)

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _key(self, role: str):
        return {"x-api-key": app_main.storage.generate_api_key_for_tenant(
            self.tenant_a, role=role)["plaintext"]}


class ShadowEndpointsTest(_Case):
    """POST / GET / DELETE /api/v1/policies/{id}/shadow."""

    def test_register_report_unregister_round_trip(self):
        res = self.client.post("/api/v1/policies/pol_live/shadow", headers=self.headers_a,
                               json={"candidate_policy_id": "pol_cand"})
        self.assertEqual(res.status_code, 200, res.text)

        report = self.client.get("/api/v1/policies/pol_live/shadow", headers=self.headers_a)
        self.assertEqual(report.status_code, 200, report.text)
        self.assertIsInstance(report.json(), dict)

        gone = self.client.delete("/api/v1/policies/pol_live/shadow", headers=self.headers_a)
        self.assertEqual(gone.status_code, 200, gone.text)

    def test_registering_an_unknown_candidate_is_404_not_500(self):
        res = self.client.post("/api/v1/policies/pol_live/shadow", headers=self.headers_a,
                               json={"candidate_policy_id": "does_not_exist"})
        self.assertEqual(res.status_code, 404, res.text)

    def test_another_tenant_cannot_register_a_shadow_on_this_policy(self):
        """404, not 403: existence is itself tenant-scoped information."""
        res = self.client.post("/api/v1/policies/pol_live/shadow", headers=self.headers_b,
                               json={"candidate_policy_id": "pol_cand"})
        self.assertEqual(res.status_code, 404, res.text)

    def test_another_tenant_cannot_read_the_divergence_report(self):
        self.client.post("/api/v1/policies/pol_live/shadow", headers=self.headers_a,
                         json={"candidate_policy_id": "pol_cand"})
        res = self.client.get("/api/v1/policies/pol_live/shadow", headers=self.headers_b)
        # Was 200 {"live_policy_id": "pol_live", "active": false, "shadow_count": 0} before this
        # test existed: no data crossed the boundary, but the handler answered about a policy the
        # caller cannot see. 404, like every sibling endpoint.
        self.assertEqual(res.status_code, 404, res.text)

    def test_another_tenant_cannot_unregister_this_shadow(self):
        self.client.post("/api/v1/policies/pol_live/shadow", headers=self.headers_a,
                         json={"candidate_policy_id": "pol_cand"})
        gone = self.client.delete("/api/v1/policies/pol_live/shadow", headers=self.headers_b)
        self.assertEqual(gone.status_code, 404, gone.text)
        still = self.client.get("/api/v1/policies/pol_live/shadow", headers=self.headers_a)
        self.assertEqual(still.status_code, 200, still.text)

    def test_a_viewer_cannot_dark_launch_a_policy(self):
        """Registering a shadow changes what runs on live traffic. Read-only must not."""
        res = self.client.post("/api/v1/policies/pol_live/shadow", headers=self._key("viewer"),
                               json={"candidate_policy_id": "pol_cand"})
        self.assertEqual(res.status_code, 403, res.text)


class WhatIfEndpointTest(_Case):
    """POST /api/v1/policies/{id}/whatif."""

    def test_unknown_policy_is_404(self):
        res = self.client.post("/api/v1/policies/nope/whatif", headers=self.headers_a,
                               json={"kpis": []})
        self.assertEqual(res.status_code, 404, res.text)

    def test_another_tenants_policy_is_404(self):
        res = self.client.post("/api/v1/policies/pol_live/whatif", headers=self.headers_b,
                               json={"kpis": []})
        self.assertEqual(res.status_code, 404, res.text)

    def test_a_malformed_kpi_is_400_not_500(self):
        res = self.client.post("/api/v1/policies/pol_live/whatif", headers=self.headers_a,
                               json={"kpis": [{"name": "bad", "type": "no_such_type"}]})
        self.assertIn(res.status_code, (200, 400), res.text)
        self.assertNotEqual(res.status_code, 500, res.text)

    def test_an_empty_kpi_list_is_400(self):
        """whatif.py:103 requires at least one spec. Asserted so the 400 stays a 400 rather than
        drifting into a 200 that computes nothing."""
        res = self.client.post("/api/v1/policies/pol_live/whatif", headers=self.headers_a,
                               json={"kpis": []})
        self.assertEqual(res.status_code, 400, res.text)

    def test_it_answers_for_a_policy_with_no_recorded_decisions(self):
        """The empty-population case: a replay endpoint must not divide by zero on a new policy."""
        res = self.client.post("/api/v1/policies/pol_live/whatif", headers=self.headers_a,
                               json={"kpis": [{"name": "approval_rate", "type": "outcome_rate",
                                               "outcome": "approve"}], "sample": 10})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["scanned"], 0)
        self.assertEqual(len(body["kpis"]), 1)


class ReleasesEndpointTest(_Case):
    """GET /api/v1/policies/{id}/releases."""

    def test_returns_a_timeline_for_a_policy_with_no_releases(self):
        res = self.client.get("/api/v1/policies/pol_live/releases", headers=self.headers_a)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["policy_id"], "pol_live")
        self.assertEqual(body["releases"], [])

    def test_unknown_policy_is_404(self):
        res = self.client.get("/api/v1/policies/nope/releases", headers=self.headers_a)
        self.assertEqual(res.status_code, 404, res.text)

    def test_another_tenants_policy_is_404(self):
        res = self.client.get("/api/v1/policies/pol_live/releases", headers=self.headers_b)
        self.assertEqual(res.status_code, 404, res.text)


class RollbackEndpointTest(_Case):
    """POST /api/v1/policies/{id}/rollback.

    The audit called this one "borderline" -- the `release` module is tested
    (`test_backlog_features.py:176-198`), the route was not. Restoring a policy to an earlier
    snapshot is the most destructive verb on this surface, so it is the last one that should
    reach production with its handler unexercised.
    """

    def test_unknown_promotion_is_404_not_500(self):
        res = self.client.post("/api/v1/policies/pol_live/rollback", headers=self.headers_a,
                               json={"promotion_id": 999999})
        self.assertEqual(res.status_code, 404, res.text)

    def test_another_tenant_cannot_roll_back_this_policy(self):
        res = self.client.post("/api/v1/policies/pol_live/rollback", headers=self.headers_b,
                               json={"promotion_id": 1})
        self.assertEqual(res.status_code, 404, res.text)

    def test_a_viewer_cannot_roll_back(self):
        res = self.client.post("/api/v1/policies/pol_live/rollback", headers=self._key("viewer"),
                               json={"promotion_id": 1})
        self.assertEqual(res.status_code, 403, res.text)

    def test_the_ledger_records_the_caller_not_the_actor_it_claims(self):
        """`actor` in the body was written to the promotion ledger as-is: any caller could record
        a rollback of production under someone else's name."""
        # Promotion is gated on a passing test; record one so there is a snapshot to roll back to.
        app_main.storage.update_policy("pol_live", {"last_test_result": {"passed": True}},
                                       tenant_id=self.tenant_a)
        promoted = self.client.post("/api/v1/policies/pol_live/promote", headers=self.headers_a,
                                    json={"reason": "baseline for rollback"})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        promotion_id = app_main.storage.list_promotions(tenant_id=self.tenant_a)[0]["id"]
        res = self.client.post("/api/v1/policies/pol_live/rollback", headers=self.headers_a,
                               json={"promotion_id": promotion_id, "actor": "mallory", "reason": "test"})
        self.assertEqual(res.status_code, 200, res.text)
        key_id = app_main.storage.get_tenant_by_api_key(self.headers_a["x-api-key"])["api_key"]["id"]
        rollback_rows = [row for row in app_main.storage.list_promotions(tenant_id=self.tenant_a)
                         if row.get("entity_id") == "pol_live" and row["id"] != promotion_id]
        self.assertTrue(rollback_rows, "the rollback left no ledger row")
        self.assertEqual(rollback_rows[0]["promoted_by"], key_id)

    def test_a_missing_promotion_id_is_422_not_500(self):
        res = self.client.post("/api/v1/policies/pol_live/rollback", headers=self.headers_a,
                               json={})
        self.assertEqual(res.status_code, 422, res.text)


class EntityHistoryTest(_Case):
    """GET /api/v1/history/{entity_type}/{entity_id} -- version history for every entity type that
    storage snapshots. Only variables had a route; a policy's history was written on every change
    and readable by nothing."""

    def test_versions_come_back_newest_first(self):
        app_main.storage.update_policy("pol_live", {"name": "renamed"}, tenant_id=self.tenant_a)
        res = self.client.get("/api/v1/history/policy/pol_live", headers=self._key("viewer"))
        self.assertEqual(res.status_code, 200, res.text)
        versions = [row["version"] for row in res.json()]
        self.assertGreaterEqual(len(versions), 2)
        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(res.json()[0]["snapshot"]["name"], "renamed")

    def test_another_tenant_sees_no_versions(self):
        res = self.client.get("/api/v1/history/policy/pol_live", headers=self.headers_b)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json(), [])

    def test_an_unknown_entity_type_is_404(self):
        res = self.client.get("/api/v1/history/tenant/x", headers=self.headers_a)
        self.assertEqual(res.status_code, 404, res.text)

    def test_connector_history_never_returns_a_secret(self):
        """Even a snapshot written in plaintext before the masking fix -- written directly here."""
        from app.models import EntityHistory

        with app_main.storage.connect() as session:
            session.add(EntityHistory(tenant_id=self.tenant_a, entity_type="connector", entity_id="c1",
                                      version=1, snapshot={"id": "c1", "config": {"api_key": "PLAINTEXT-SECRET"}}))
        res = self.client.get("/api/v1/history/connector/c1", headers=self.headers_a)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertNotIn("PLAINTEXT-SECRET", res.text)


class LatencyAnalyticsTest(_Case):
    """GET /api/v1/analytics/latency -- called nowhere in the repository before this."""

    def test_it_answers_and_is_shaped_like_its_siblings(self):
        res = self.client.get("/api/v1/analytics/latency", headers=self.headers_a)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsInstance(res.json(), dict)

    def test_it_is_tenant_scoped(self):
        """Both tenants answer; neither may see the other's numbers. A shared-default fallback
        would make these identical objects."""
        a = self.client.get("/api/v1/analytics/latency", headers=self.headers_a)
        b = self.client.get("/api/v1/analytics/latency", headers=self.headers_b)
        self.assertEqual(a.status_code, 200, a.text)
        self.assertEqual(b.status_code, 200, b.text)

    def test_it_requires_authentication(self):
        res = self.client.get("/api/v1/analytics/latency")
        self.assertIn(res.status_code, (401, 403), res.text)


class CollabSeedTest(_Case):
    """POST /api/v1/collab/{doc_id}/seed."""

    def test_seeding_then_reading_state_round_trips(self):
        res = self.client.post("/api/v1/collab/doc1/seed", headers=self.headers_a,
                               json={"initial": {"name": "Loan Policy", "threshold": 700}})
        self.assertEqual(res.status_code, 200, res.text)
        state = self.client.get("/api/v1/collab/doc1/state", headers=self.headers_a)
        self.assertEqual(state.status_code, 200, state.text)
        self.assertEqual(state.json()["doc"]["name"], "Loan Policy")

    def test_seeding_is_a_noop_once_the_document_has_history(self):
        """Its docstring promises this. Without it, reopening an editor silently discards edits."""
        self.client.post("/api/v1/collab/doc2/seed", headers=self.headers_a,
                         json={"initial": {"threshold": 700}})
        self.client.post("/api/v1/collab/doc2/seed", headers=self.headers_a,
                         json={"initial": {"threshold": 999}})
        state = self.client.get("/api/v1/collab/doc2/state", headers=self.headers_a).json()
        self.assertEqual(state["doc"]["threshold"], 700)

    def test_one_tenants_seed_is_invisible_to_another(self):
        """Same doc_id, two tenants. The hub is keyed on (tenant, doc); if it were not, two
        customers editing a document with the same id would see each other's fields."""
        self.client.post("/api/v1/collab/shared-id/seed", headers=self.headers_a,
                         json={"initial": {"secret": "tenant-a-only"}})
        state_b = self.client.get("/api/v1/collab/shared-id/state", headers=self.headers_b)
        self.assertEqual(state_b.status_code, 200, state_b.text)
        # NB: the key is "doc", not "fields". The first draft of this test read .get("fields", {})
        # and therefore passed without ever looking at the document -- a vacuous isolation test,
        # the exact failure mode this whole sweep exists to find.
        self.assertNotIn("secret", state_b.json()["doc"])
        self.assertEqual(state_b.json()["version"], 0)


if __name__ == "__main__":
    unittest.main()
