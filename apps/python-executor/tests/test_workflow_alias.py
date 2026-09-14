"""A sub-workflow can be named by a stable alias, and an alias that does not resolve fails
CLOSED (parity P08).

The `workflow` step type let one policy compose another, but only by raw policy id. That is fine
until the child moves: a new version in dev, a promotion to prod, a workspace imported from a
bundle where the child's id differs. Every parent that names the child has to be edited, and a
parent that is missed keeps calling the old one -- silently, because a policy id that no longer
exists is not distinguishable at authoring time from one that does.

WHY THE FAILURE DIRECTION MATTERS MORE THAN THE FEATURE. `_run_steps` catches a step exception,
records it on the trace and CONTINUES unless the step opted into `onFailure: abort`. A sub-workflow
is not an ordinary step: it is a bundle of gates, and the errors that escape it -- an unregistered
alias, a dangling id, a cycle, depth exhaustion -- all mean NONE of those gates ran. So a parent
whose child was supposed to reject finished with the rejection simply absent:

    parent: [ run KYC child (which rejects) , outcome: approve ]
    child unreachable -> child's reject never happens -> parent APPROVES

That is the same fail-open shape as a variable that could not compute (Tier 1.2): HTTP 200, an
approval, and one line on a trace nobody reads. `workflow` now sits alongside rule / scorecard /
decision_table in the fail-closed set, so a child that could not run sends the decision to review.
`FailsClosedTest` is the guard on that.

The cycle guard also had to move. It keys on the RESOLVED policy id rather than on whatever name
the step used, because two aliases can point at the same policy -- guard the name and A -> B -> A
walks straight back in through a second spelling of B.
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
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import fast_decide  # noqa: E402
from app.storage import Storage  # noqa: E402


def outcome_policy(policy_id, name, outcome):
    """A child that does nothing but land on an outcome — so the parent's result says
    unambiguously WHICH child ran, or whether one ran at all."""
    return {"id": policy_id, "name": name, "defaultOutcome": "review",
            "steps": [{"id": "s1", "type": "outcome", "ref_id": outcome, "label": outcome}]}


def parent_policy(child_step, policy_id="pol_parent"):
    """Parent shape chosen to expose the failure direction: run the child, then approve.

    `_merge_outcome` keeps the more severe outcome, so when the child rejects the parent rejects.
    When the child does not run at all, the trailing approve is the only thing left -- which is
    exactly the fail-open this guards.
    """
    return {"id": policy_id, "name": "Parent", "defaultOutcome": "review",
            "steps": [child_step, {"id": "s2", "type": "outcome", "ref_id": "approve", "label": "approve"}]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "alias.db"))
        self.tenant = app_main.storage.default_tenant_id
        app_main.storage.create_policy(outcome_policy("pol_child_v1", "Child v1", "reject"),
                                       tenant_id=self.tenant)
        app_main.storage.create_policy(outcome_policy("pol_child_v2", "Child v2", "approve"),
                                       tenant_id=self.tenant)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()
        fast_decide.invalidate()

        # Restore the global, and release this test's engine. Placed at the END of tearDown so
        # the teardown above still runs against THIS test's storage -- an earlier version put it
        # first and silently redirected those statements at the restored global.
        #
        # The restore is the load-bearing half. Leaving app_main.storage pointed at a deleted
        # tempdir made an UNRELATED module later fail with
        # `sqlite3.OperationalError: unable to open database file`: the failure lands on whoever
        # reads the global next, never on the test that leaked it, which is why it looked like a
        # random flake for so long.
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        try:
            app_main.storage = self._orig_storage
        except Exception:  # pragma: no cover
            pass

    def register(self, aliases):
        """Write the alias registry.

        It lives in `engine_config.workflow_aliases` on tenant settings — the same per-tenant
        settings pattern as `engine_config.shadow_map`. Invalidating afterwards is not cosmetic:
        the executor reads its catalog from fast_decide's cross-request cache, whose 5s TTL
        would otherwise serve the previous registry to the very next decision.
        """
        settings = app_main.storage.get_settings(tenant_id=self.tenant)
        engine_config = dict(settings.get("engine_config") or {})
        engine_config["workflow_aliases"] = aliases
        app_main.storage.update_settings({"engine_config": engine_config}, tenant_id=self.tenant)
        fast_decide.invalidate()

    def build_parent(self, step, policy_id="pol_parent"):
        app_main.storage.create_policy(parent_policy(step, policy_id), tenant_id=self.tenant)

    def decide(self, policy_id="pol_parent"):
        response = self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policyId": policy_id, "payload": {}})
        self.assertEqual(response.status_code, 200, response.text[:300])
        return response.json()

    @staticmethod
    def workflow_step(body):
        return next(s for s in body["trace"] if s.get("step", {}).get("type") == "workflow")


class ResolvesByAliasTest(_Base):
    def test_a_plain_ref_id_child_still_runs(self):
        """The regression guard. Alias resolution is additive; every existing parent names a
        raw id and must keep working untouched."""
        self.build_parent({"id": "s1", "type": "workflow", "ref_id": "pol_child_v1"})
        self.assertEqual(self.decide()["outcome"], "reject")

    def test_a_child_named_by_alias_runs(self):
        self.register({"kyc_check": "pol_child_v1"})
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "kyc_check"}})
        body = self.decide()
        self.assertEqual(body["outcome"], "reject", "the aliased child did not run")
        self.assertEqual(self.workflow_step(body)["result"]["workflow"], "pol_child_v1")
        self.assertEqual(self.workflow_step(body)["result"]["alias"], "kyc_check",
                         "the trace must record which alias was followed, not just where it landed")

    def test_repointing_the_alias_reroutes_the_parent_untouched(self):
        """The whole point. The child moves; the parent's stored steps do not change at all."""
        self.register({"kyc_check": "pol_child_v1"})
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "kyc_check"}})
        self.assertEqual(self.decide()["outcome"], "reject")

        before = app_main.storage.get_policy("pol_parent", tenant_id=self.tenant)["steps"]
        self.register({"kyc_check": "pol_child_v2"})
        self.assertEqual(self.decide()["outcome"], "approve", "repointing the alias had no effect")
        after = app_main.storage.get_policy("pol_parent", tenant_id=self.tenant)["steps"]
        self.assertEqual(before, after, "the parent policy was rewritten; the alias exists to avoid that")

    def test_an_alias_can_resolve_differently_per_environment(self):
        """dev -> uat -> prod is the promotion the feature is named for: the parent calls
        `kyc_check`, and which child that is depends on the environment it is running in."""
        self.register({"kyc_check": {"dev": "pol_child_v2", "prod": "pol_child_v1"}})
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "kyc_check"}})
        self.assertEqual(self.decide()["outcome"], "approve", "a dev policy did not take the dev arm")

        app_main.storage.update_policy("pol_parent", {"status": "prod"}, tenant_id=self.tenant)
        fast_decide.invalidate()
        self.assertEqual(self.decide()["outcome"], "reject", "a prod policy did not take the prod arm")

    def test_an_environment_falls_back_to_default(self):
        self.register({"kyc_check": {"default": "pol_child_v1", "prod": "pol_child_v2"}})
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "kyc_check"}})
        self.assertEqual(self.decide()["outcome"], "reject")

    def test_the_alias_wins_over_a_stale_ref_id(self):
        """A step that names an alias has opted into indirection. Falling back to a hard-coded id
        when the registry answers differently would reinstate the exact drift the alias prevents —
        and would do it silently, which is worse than not having the feature."""
        self.register({"kyc_check": "pol_child_v2"})
        self.build_parent({"id": "s1", "type": "workflow", "ref_id": "pol_child_v1",
                           "config": {"alias": "kyc_check"}})
        self.assertEqual(self.decide()["outcome"], "approve")


class FailsClosedTest(_Base):
    """An alias that resolves to nothing must not mean 'skip the child and carry on'."""

    def test_an_unregistered_alias_does_not_approve(self):
        """THE DIRECTION TEST. The child rejects; the parent then approves. With the child
        unreachable and a sub-workflow treated as an ordinary step, the parent returned
        `approve` — HTTP 200, no error surfaced to the caller, an applicant the child was
        composed in to stop."""
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "not_registered"}})
        body = self.decide()
        self.assertNotEqual(body["outcome"], "approve",
                            "a child policy that never ran approved the decision it was meant to gate")
        self.assertEqual(body["outcome"], "review", "the safe landing spot is a human, not a guess")

    def test_the_caller_can_see_why(self):
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "not_registered"}})
        error = self.workflow_step(self.decide())["error"]
        self.assertIn("not_registered", error)
        self.assertIn("workflow_aliases", error, "the error should say where to register it")

    def test_it_is_alertable(self):
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "not_registered"}})
        self.decide()
        events = app_main.storage.list_error_events(tenant_id=self.tenant)
        self.assertTrue([e for e in events if e.get("stage") == "gate_error"],
                        "an unreachable sub-workflow produced no error event")

    def test_an_unregistered_alias_does_not_fall_back_to_the_stale_ref_id(self):
        """The tempting "be helpful" bug. The step carries both, so falling back looks like
        graceful degradation — but the alias was added precisely because the id went stale, and
        an author who de-registers an alias to retire a child would find every parent quietly
        still calling it."""
        self.build_parent({"id": "s1", "type": "workflow", "ref_id": "pol_child_v2",
                           "config": {"alias": "not_registered"}})
        body = self.decide()
        self.assertEqual(body["outcome"], "review",
                         "the step quietly ran the stale ref_id instead of failing")
        self.assertIsNone(self.workflow_step(body)["result"], "a child ran that nobody named")
        self.assertIn("not_registered", self.workflow_step(body)["error"])

    def test_an_alias_pointing_at_a_missing_policy_fails_closed(self):
        self.register({"kyc_check": "pol_deleted"})
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "kyc_check"}})
        body = self.decide()
        self.assertEqual(body["outcome"], "review")
        self.assertIn("pol_deleted", self.workflow_step(body)["error"])

    def test_a_dangling_raw_ref_id_fails_closed_too(self):
        """Same hole, reached the old way — it was never alias-specific."""
        self.build_parent({"id": "s1", "type": "workflow", "ref_id": "pol_deleted"})
        self.assertEqual(self.decide()["outcome"], "review")

    def test_another_tenants_policy_cannot_be_reached(self):
        """Not 'checked and refused' — unreachable by construction. The registry is read from
        THIS tenant's settings and the id it yields is loaded with a tenant-scoped get_policy, so
        naming a foreign id resolves to nothing exactly like naming a deleted one. There is no
        code path on which a cross-tenant id could be loaded, which is the only version of this
        guarantee worth having."""
        other = app_main.storage.create_tenant("Other Workspace")
        app_main.storage.create_policy(outcome_policy("pol_secret", "Secret", "approve"),
                                       tenant_id=other["id"])
        self.register({"kyc_check": "pol_secret"})
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "kyc_check"}})
        body = self.decide()
        self.assertEqual(body["outcome"], "review")
        self.assertIn("Unknown sub-workflow", self.workflow_step(body)["error"])
        # And the foreign policy is genuinely still there — this failed on tenant scoping, not
        # because the fixture was wrong.
        self.assertIsNotNone(app_main.storage.get_policy("pol_secret", tenant_id=other["id"]))


class CycleGuardTest(_Base):
    """Indirection is a new way to build a loop, so the guard has to key on where a step LANDS,
    not on what it was called."""

    def test_a_cycle_through_two_different_aliases_is_detected(self):
        """parent --alias 'child'--> child --alias 'back'--> parent.

        Neither alias name repeats, so a guard that remembered names would see nothing wrong and
        recurse until the depth limit — ten nested executions of the same policies per request.
        """
        self.register({"child": "pol_child_cyclic", "back": "pol_parent"})
        app_main.storage.create_policy(
            {"id": "pol_child_cyclic", "name": "Cyclic Child", "defaultOutcome": "review",
             "steps": [{"id": "c1", "type": "workflow", "config": {"alias": "back"}}]},
            tenant_id=self.tenant)
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "child"}})
        body = self.decide()
        self.assertEqual(body["outcome"], "review", "a cyclic composition approved")
        errors = [s.get("error") or "" for s in body["trace"]]
        self.assertTrue(any("cycle detected: pol_parent" in e for e in errors),
                        "the cycle must be reported against the POLICY it re-entered, not the "
                        "alias that named it: {0}".format(errors))
        # A name-keyed guard still trips EVENTUALLY -- on the second lap, when an alias finally
        # repeats -- so "an error was raised" is not the property worth asserting. This is: the
        # loop was cut on first re-entry, and no policy in it ran twice.
        outcomes = [s for s in body["trace"] if s.get("step", {}).get("type") == "outcome"]
        self.assertEqual(len(outcomes), 1,
                         "the parent's steps executed more than once before the cycle was cut")

    def test_two_aliases_for_the_same_child_are_one_visit(self):
        """`primary` and `secondary` both name pol_child_v1, so the second call is a revisit even
        though the name is new."""
        self.register({"primary": "pol_child_v1", "secondary": "pol_child_v1"})
        app_main.storage.update_policy(
            "pol_child_v1",
            {"steps": [{"id": "c1", "type": "outcome", "ref_id": "reject"},
                       {"id": "c2", "type": "workflow", "config": {"alias": "secondary"}}]},
            tenant_id=self.tenant)
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "primary"}})
        errors = [s.get("error") or "" for s in self.decide()["trace"]]
        self.assertTrue(any("cycle detected: pol_child_v1" in e for e in errors),
                        "a second alias for a visited policy was not treated as a revisit: {0}".format(errors))

    def test_an_alias_and_a_raw_id_for_the_same_child_are_one_visit(self):
        self.register({"child": "pol_child_v1"})
        app_main.storage.update_policy(
            "pol_child_v1",
            {"steps": [{"id": "c1", "type": "outcome", "ref_id": "reject"},
                       {"id": "c2", "type": "workflow", "ref_id": "pol_child_v1"}]},
            tenant_id=self.tenant)
        self.build_parent({"id": "s1", "type": "workflow", "config": {"alias": "child"}})
        errors = [s.get("error") or "" for s in self.decide()["trace"]]
        self.assertTrue(any("cycle detected" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
