"""Sweep: the governance settings endpoints, and what they are supposed to CAUSE.

`/api/v1/settings/governance` (GET + PUT) is mentioned nowhere in `tests/`. `test_dual_control.py`
exercises the control itself, but it arms it by writing `engine_config` through storage directly --
so the endpoint an admin actually uses to turn two-person control on had nothing asserting that
flipping it changes anything. A toggle that returns 200 and gates nothing is the worst kind of
security control, because the UI says it is on.

Also covered here:

* **`PUT /api/v1/settings` replaces `engine_config` wholesale.** `storage.update_settings` merges
  at the TOP level only (`next_value.update(...)`), so a patch naming `engine_config` swaps the
  whole blob. Every governance setting lives inside that blob -- `require_dual_control`,
  `pii_redact_keys`, `slo`, the shadow registry -- and the four endpoints that own those keys each
  read-modify-write it. Saving the generic Settings page from a stale load therefore turns
  two-person control OFF silently, with no audit event and no diff. The dashboard happens to spread
  the object it last loaded, so it usually round-trips; "usually" is doing a lot of work there.

* **Who may flip it.** The docstring says admin-only. `/api/v1/settings` maps to MANAGE_ACCESS, so
  a `policy_maker` -- the role that authors the policies dual control exists to gate -- must not be
  able to turn it off. That is the assertion that makes the docstring enforceable.

* **Tenant scoping on /settings/governance and /usage/history.**
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


class _GovernanceCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "gov.db"))
        self.client = TestClient(app_main.app)
        self.tenant_a = app_main.storage.default_tenant_id
        self.headers_a = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_b = app_main.storage.create_tenant("Sweep Governance Co")["id"]
        self.headers_b = {
            "x-api-key": app_main.storage.generate_api_key_for_tenant(self.tenant_b)["plaintext"]
        }

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _key(self, role: str):
        return {"x-api-key": app_main.storage.generate_api_key_for_tenant(
            self.tenant_a, role=role)["plaintext"]}

    def _set_dual_control(self, on: bool, headers=None):
        return self.client.put("/api/v1/settings/governance",
                               headers=headers or self.headers_a,
                               json={"require_dual_control": on})


class DualControlToggleTest(_GovernanceCase):
    """PUT /api/v1/settings/governance must arm the control it advertises."""

    def _rule_in_uat(self, rule_id: str) -> str:
        app_main.storage.create_rule(
            {"id": rule_id, "name": rule_id, "status": "dev", "nodes": [],
             "last_test_result": {"passed": True}}, tenant_id=self.tenant_a)
        res = self.client.post("/api/v1/deploy/promote", headers=self.headers_a,
                               json={"items": [{"entity_type": "rule", "entity_id": rule_id}]})
        self.assertEqual(res.status_code, 200, res.text)
        return rule_id

    def test_the_toggle_round_trips(self):
        self.assertFalse(self.client.get("/api/v1/settings/governance",
                                         headers=self.headers_a).json()["require_dual_control"])
        self.assertTrue(self._set_dual_control(True).json()["require_dual_control"])
        self.assertTrue(self.client.get("/api/v1/settings/governance",
                                        headers=self.headers_a).json()["require_dual_control"])

    def test_turning_it_on_blocks_a_same_actor_production_promotion(self):
        """The contract that matters: a 200 from this endpoint has to change a promotion.

        Same API key promotes to UAT and then to prod. With the toggle off that is allowed; with
        it on it must be a 403, because maker and checker are the same actor.
        """
        rule_id = self._rule_in_uat("gov-dual")
        self._set_dual_control(True)
        res = self.client.post("/api/v1/deploy/promote", headers=self.headers_a,
                               json={"items": [{"entity_type": "rule", "entity_id": rule_id}]})
        self.assertEqual(res.status_code, 403, res.text)
        self.assertEqual(app_main.storage.get_rule(rule_id, tenant_id=self.tenant_a)["status"], "uat")

    def test_leaving_it_off_still_allows_the_promotion(self):
        """Regression guard: the control must stay OPT-IN. Arming it by default would start
        refusing promotions on upgrade for every workspace that never chose it."""
        rule_id = self._rule_in_uat("gov-nodual")
        res = self.client.post("/api/v1/deploy/promote", headers=self.headers_a,
                               json={"items": [{"entity_type": "rule", "entity_id": rule_id}]})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(app_main.storage.get_rule(rule_id, tenant_id=self.tenant_a)["status"], "prod")


class GovernanceSettingDurabilityTest(_GovernanceCase):
    """The governance flag shares one JSON blob with every other engine setting."""

    def test_a_generic_settings_save_must_not_silently_disarm_dual_control(self):
        """`PUT /api/v1/settings {"engine_config": {...}}` replaces the blob, flag and all.

        An admin editing the Python version on the Settings page can turn two-person control off
        without being told, and nothing records that it happened -- no audit event, no diff, and
        the governance page simply reads `false` afterwards as though it was never on.
        """
        self._set_dual_control(True)
        self.client.put("/api/v1/settings", headers=self.headers_a,
                        json={"engine_config": {"python_version": "3.11"}})
        self.assertTrue(
            self.client.get("/api/v1/settings/governance",
                            headers=self.headers_a).json()["require_dual_control"],
            "an unrelated settings save turned two-person control off",
        )

    def test_a_generic_settings_save_still_applies_what_it_names(self):
        """Regression guard for the merge: preserving the governance keys must not make the
        generic endpoint a no-op for the keys it does name.

        The values here are deliberately NOT the seeded defaults ("3.11"/2000/128). Written
        with the default first, this test survived a mutation that dropped the incoming patch
        entirely -- it was asserting the value the fixture already held.
        """
        self.client.put("/api/v1/settings", headers=self.headers_a,
                        json={"engine_config": {"python_version": "3.13", "timeout_ms": 777}})
        engine = self.client.get("/api/v1/settings", headers=self.headers_a).json()["engine_config"]
        self.assertEqual(engine["python_version"], "3.13")
        self.assertEqual(engine["timeout_ms"], 777)
        self.assertEqual(engine["memory_mb"], 128, "an unnamed sibling key was dropped")

    def test_disarming_dual_control_is_audited(self):
        """Turning the control OFF is itself a change-management event. It used to leave no
        trace at all -- the promotions it stopped gating were logged, the decision to stop
        gating them was not."""
        self._set_dual_control(True)
        self._set_dual_control(False)
        events = self.client.get("/api/v1/audit", headers=self.headers_a,
                                 params={"event_type": "governance_changed"}).json()
        self.assertEqual(len(events), 2, "arming/disarming dual control wrote no audit trail")
        self.assertEqual(events[0]["metadata"]["to"], False)
        self.assertEqual(events[0]["metadata"]["from"], True)

    def test_a_no_op_toggle_does_not_pad_the_audit_trail(self):
        """Regression guard: a page that PUTs its current state on every save must not fill the
        compliance log with events describing no change."""
        self._set_dual_control(True)
        self._set_dual_control(True)
        events = self.client.get("/api/v1/audit", headers=self.headers_a,
                                 params={"event_type": "governance_changed"}).json()
        self.assertEqual(len(events), 1)

    def test_the_sibling_settings_endpoints_do_not_clobber_each_other(self):
        """Regression guard: data-protection, SLO and governance each read-modify-write
        engine_config. Applied in sequence, all three must survive."""
        self.client.put("/api/v1/settings/data-protection", headers=self.headers_a,
                        json={"pii_redact_keys": ["pan"]})
        self._set_dual_control(True)
        self.client.put("/api/v1/settings/slo", headers=self.headers_a,
                        json={"latency_p95_ms": 250.0})
        self.assertEqual(self.client.get("/api/v1/settings/data-protection",
                                         headers=self.headers_a).json()["pii_redact_keys"], ["pan"])
        self.assertTrue(self.client.get("/api/v1/settings/governance",
                                        headers=self.headers_a).json()["require_dual_control"])
        self.assertEqual(self.client.get("/api/v1/settings/slo",
                                         headers=self.headers_a).json()["latency_p95_ms"], 250.0)


class GovernanceAuthorizationTest(_GovernanceCase):
    """Only a workspace admin may change the change-management posture."""

    def test_a_policy_maker_cannot_disarm_dual_control(self):
        """The role that authors the policies dual control gates must not be able to turn it
        off. /api/v1/settings maps to MANAGE_ACCESS; this is that mapping's consequence."""
        self._set_dual_control(True)
        res = self._set_dual_control(False, headers=self._key("policy_maker"))
        self.assertEqual(res.status_code, 403)
        self.assertTrue(self.client.get("/api/v1/settings/governance",
                                        headers=self.headers_a).json()["require_dual_control"])

    def test_a_viewer_cannot_change_it(self):
        res = self._set_dual_control(True, headers=self._key("viewer"))
        self.assertEqual(res.status_code, 403)

    def test_a_viewer_may_still_read_the_posture(self):
        """Regression guard: it is a READ, so read-only roles must keep it."""
        res = self.client.get("/api/v1/settings/governance", headers=self._key("viewer"))
        self.assertEqual(res.status_code, 200)


class GovernanceTenantScopeTest(_GovernanceCase):
    def test_the_governance_posture_is_per_tenant(self):
        self._set_dual_control(True)
        self.assertFalse(self.client.get("/api/v1/settings/governance",
                                         headers=self.headers_b).json()["require_dual_control"],
                         "tenant A's governance posture was reported to tenant B")

    def test_usage_history_is_per_tenant(self):
        from app import quota

        app_main.storage.record_usage(quota.METRIC_DECISIONS, 7, tenant_id=self.tenant_a)
        self.assertEqual(self.client.get("/api/v1/usage/history",
                                         headers=self.headers_a).json()["total"], 7)
        self.assertEqual(self.client.get("/api/v1/usage/history",
                                         headers=self.headers_b).json()["total"], 0,
                         "tenant B's usage history counted tenant A's decisions")

    def test_usage_history_rejects_an_out_of_range_window(self):
        for days in (0, -1, 10_000):
            with self.subTest(days=days):
                self.assertEqual(
                    self.client.get("/api/v1/usage/history", params={"days": days},
                                    headers=self.headers_a).status_code, 422)

    def test_policy_lifecycle_is_tenant_scoped(self):
        """GET /api/v1/policies/{id}/lifecycle calls get_policy with NO tenant_id, so it rests
        entirely on the storage fallback chain. test_lifecycle.py covers the transitions; it
        never looks at a second tenant."""
        app_main.storage.create_policy(
            {"id": "gov-pol", "name": "gov-pol", "status": "dev", "steps": []},
            tenant_id=self.tenant_a)
        self.assertEqual(self.client.get("/api/v1/policies/gov-pol/lifecycle",
                                         headers=self.headers_a).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/policies/gov-pol/lifecycle",
                                         headers=self.headers_b).status_code, 404,
                         "tenant B read tenant A's policy lifecycle")

    def test_a_missing_policy_lifecycle_is_404(self):
        self.assertEqual(self.client.get("/api/v1/policies/no-such/lifecycle",
                                         headers=self.headers_a).status_code, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
