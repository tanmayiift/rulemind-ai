"""Step-through debugging for a policy run (parity P04).

`ctx.step_trace` is POST-HOC -- what already happened, after the decision is over -- and
`debug_loop` re-evaluates one loop step in isolation. Neither lets someone stop before a step,
inspect the variables and outcome AS THEY STOOD THERE, and advance one step. That is the
difference between reading a flight recorder and flying the plane.

TWO PROPERTIES CARRY THIS FEATURE, and they are what most of these tests are about:

  1. STEPPING USES THE REAL EXECUTION PATH. A debugger whose answers differ from production is
     worse than no debugger, so stepping through a policy must reach the same outcome as running
     it normally. `test_stepping_reaches_the_same_outcome_as_a_normal_run` is the one to keep
     working -- it is the entire justification for reusing `paused_at_step` instead of writing a
     separate debug interpreter.
  2. STEPPING HAS NO SIDE EFFECTS. It runs with simulate=True, so it writes no Decision row,
     meters no billable unit, fires no outbound action and creates no review task. Without that,
     an operator debugging one failure bills the workspace once per click.
"""
from __future__ import annotations

import asyncio
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
from app.executor import PolicyExecutor  # noqa: E402
from app.routers import debugger as debugger_router  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import restore_storage_after  # noqa: E402

BASE = "/api/v1/debug/sessions"

VARIABLE_CODE = (
    '@variable(source="custom")\n'
    "def dbg_score(payload, variables, apis):\n"
    "    return int(payload.get(\"score\") or 0)\n"
)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "dbg.db"))
        restore_storage_after(self)
        self.storage = app_main.storage
        self.client = TestClient(app_main.app)
        self.tenant = self.storage.default_tenant_id
        self.key = self.storage.default_api_key or ""
        debugger_router._SESSIONS.clear()
        os.environ.pop("AUTH_MODE", None)

        self.storage.create_variable(
            {"id": "dbg_score", "name": "dbg score", "category": "custom", "source_id": "custom",
             "code": VARIABLE_CODE, "status": "prod"}, tenant_id=self.tenant)
        self.storage.create_rule(
            {"id": "dbg_rule", "name": "gate", "status": "prod", "outcome": "approve",
             "nodes": [{"id": "n1", "type": "condition", "variable": "dbg_score",
                        "operator": ">", "value": "10"},
                       {"id": "n2", "type": "approve"}]}, tenant_id=self.tenant)
        self.policy = self.storage.create_policy(
            {"id": "dbg_policy", "name": "Debug policy", "status": "prod",
             "steps": [
                 {"id": "s1", "type": "rule", "ref_id": "dbg_rule"},
                 {"id": "s2", "type": "outcome", "ref_id": "approve"},
             ],
             "defaultOutcome": "review"}, tenant_id=self.tenant)

    def tearDown(self):
        debugger_router._SESSIONS.clear()
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

    def start(self, payload=None, policy_id="dbg_policy"):
        # `payload if payload is None else` -- NOT `payload or`. An empty payload is the most
        # interesting case a debugger has (it is what a broken integration sends), and it is
        # falsy, so `or` silently substituted the approving default and the parity test compared
        # a seeded run against a real one. It cost an hour and the "divergence" was this line.
        body = {"score": 50} if payload is None else payload
        response = self.client.post(BASE, headers=self.as_(),
                                    json={"policy_id": policy_id, "payload": body})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def step(self, sid):
        return self.client.post("{0}/{1}/step".format(BASE, sid), headers=self.as_()).json()


class SteppingTest(_Base):

    def test_a_session_starts_paused_before_the_first_step(self):
        snap = self.start()
        self.assertEqual(snap["status"], "paused")
        self.assertEqual(snap["paused_before_index"], 0)
        self.assertEqual(snap["executed_steps"], 0, "a step ran before the caller asked")
        self.assertEqual(snap["step_count"], 2)

    def test_it_says_what_it_is_about_to_do(self):
        """A post-hoc trace can only say what HAPPENED. The point of stopping is to see what
        comes next while you can still change your mind about looking."""
        snap = self.start()
        self.assertIsNotNone(snap["next_step"])
        self.assertEqual(snap["next_step"]["id"], "s1")

    def test_each_step_advances_exactly_one(self):
        snap = self.start()
        first = self.step(snap["session_id"])
        self.assertEqual(first["executed_steps"], 1)
        self.assertEqual(first["paused_before_index"], 1)
        self.assertEqual(first["next_step"]["id"], "s2")

    def test_stepping_to_the_end_finishes_the_run(self):
        snap = self.start()
        sid = snap["session_id"]
        self.step(sid)
        final = self.step(sid)
        self.assertTrue(final["finished"])
        self.assertNotEqual(final["status"], "paused")

    def test_stepping_past_the_end_is_a_no_op_not_an_error(self):
        """A UI that polls should not have to special-case its own success."""
        snap = self.start()
        sid = snap["session_id"]
        self.step(sid); self.step(sid)
        again = self.step(sid)
        self.assertTrue(again["finished"])

    def test_run_completes_from_wherever_it_is_paused(self):
        snap = self.start()
        sid = snap["session_id"]
        self.step(sid)
        done = self.client.post("{0}/{1}/run".format(BASE, sid), headers=self.as_()).json()
        self.assertTrue(done["finished"])
        self.assertEqual(done["outcome"], "approve")


class ItMatchesProductionTest(_Base):
    """The justification for reusing the real execution path instead of a debug interpreter."""

    def normal_outcome(self, payload):
        """A PRODUCTION run: simulate=False, source="api".

        It used to pass simulate=True, and that quietly gutted the comparison. simulate forces
        `_source = "simulation"`, which is in `_SAMPLE_SEEDING_SOURCES` -- so both sides of the
        assertion were seeded runs and the test could not see the divergence it exists to catch.
        This fixture's connector has no sample_payload, so seeding is a no-op HERE and the test
        looked fine; against the demo inventory the debugger showed made-up values for every
        field the caller did not send. `test_it_does_not_invent_inputs_the_caller_did_not_send`
        below is the one that pins the actual behaviour, because this one structurally cannot.
        """
        ctx = asyncio.run(PolicyExecutor(self.storage).execute(
            policy=self.policy, payload=payload, tenant_id=self.tenant,
            source="api", simulate=False))
        return ctx.outcome

    def stepped_outcome(self, payload):
        snap = self.start(payload=payload)
        sid = snap["session_id"]
        for _ in range(snap["step_count"] + 1):
            snap = self.step(sid)
            if snap["finished"]:
                break
        return snap["outcome"]

    def test_stepping_reaches_the_same_outcome_as_a_normal_run(self):
        for payload in ({"score": 50}, {"score": 1}, {}):
            with self.subTest(payload=payload):
                self.assertEqual(self.stepped_outcome(payload), self.normal_outcome(payload),
                                 "the debugger disagrees with production for {0}".format(payload))

    def test_it_does_not_invent_inputs_the_caller_did_not_send(self):
        """The divergence that two simulate=True runs structurally cannot see.

        `simulate=True` forces `_source="simulation"`, which seeds every unsupplied field from
        the connectors' demo payloads. An operator replaying a rejected application would then be
        shown a bureau score of 900 that production never had -- and would go change a threshold
        that was never the problem, because the MISSING FIELD was the bug.

        It uses `bureau`, not the fixture's `custom` source, and that is load-bearing:
        `_payloads_by_source` ends with `normalized["custom"] = flat_fields or ctx.payload`, which
        overwrites custom's seed unconditionally. A test written against `custom` passes whether
        or not seeding is on -- it was, and it did, until the mutation refused to die.
        """
        self.assertIsNotNone(
            self.storage.update_connector(
                "bureau", {"sample_payload": {"dbg_bureau_score": 900}}, tenant_id=self.tenant),
            "this test needs a non-custom connector with a sample to seed FROM")
        self.storage.create_variable(
            {"id": "dbg_bureau", "name": "dbg bureau", "category": "bureau", "source_id": "bureau",
             "code": ('@variable(source="bureau")\n'
                      "def dbg_bureau(payload, variables, apis):\n"
                      "    return int(payload.get(\"dbg_bureau_score\") or 0)\n"),
             "status": "prod"}, tenant_id=self.tenant)
        self.storage.create_rule(
            {"id": "dbg_bureau_rule", "name": "bureau gate", "status": "prod", "outcome": "approve",
             "nodes": [{"id": "b1", "type": "condition", "variable": "dbg_bureau",
                        "operator": ">", "value": "800"},
                       {"id": "b2", "type": "approve"}]}, tenant_id=self.tenant)
        self.storage.create_policy(
            {"id": "dbg_bureau_policy", "name": "Bureau policy", "status": "prod",
             "steps": [{"id": "b_s1", "type": "rule", "ref_id": "dbg_bureau_rule"}],
             "defaultOutcome": "reject"}, tenant_id=self.tenant)

        snap = self.start(payload={}, policy_id="dbg_bureau_policy")
        stepped = self.step(snap["session_id"])
        self.assertEqual(stepped["variables"].get("dbg_bureau"), 0,
                         "the debugger seeded a bureau score the caller never sent")

        # And the outcome that follows from it must be production's, not simulation's.
        while not stepped["finished"]:
            stepped = self.step(snap["session_id"])
        policy = self.storage.get_policy("dbg_bureau_policy", tenant_id=self.tenant)
        production = asyncio.run(PolicyExecutor(self.storage).execute(
            policy=policy, payload={}, tenant_id=self.tenant, source="api", simulate=False))
        self.assertEqual(stepped["outcome"], production.outcome,
                         "the debugger disagrees with production on an empty payload")

    def test_the_variables_it_shows_are_the_real_computed_ones(self):
        snap = self.start(payload={"score": 77})
        stepped = self.step(snap["session_id"])
        self.assertEqual(stepped["variables"].get("dbg_score"), 77)


class NoSideEffectsTest(_Base):
    """Stepping must not bill, log, or fire anything. An operator debugging one failure would
    otherwise cost the workspace a decision per click."""

    def test_stepping_writes_no_decision(self):
        snap = self.start()
        sid = snap["session_id"]
        self.step(sid); self.step(sid)
        self.assertEqual(self.storage.list_decisions(tenant_id=self.tenant), [],
                         "stepping logged a decision")

    def test_running_to_completion_writes_no_decision_either(self):
        snap = self.start()
        self.client.post("{0}/{1}/run".format(BASE, snap["session_id"]), headers=self.as_())
        self.assertEqual(self.storage.list_decisions(tenant_id=self.tenant), [])

    def test_a_normal_run_still_does_write_one(self):
        """The control. Without it, a build that never writes decisions at all would pass the
        two tests above."""
        asyncio.run(PolicyExecutor(self.storage).execute(
            policy=self.policy, payload={"score": 50}, tenant_id=self.tenant, source="api"))
        self.assertEqual(len(self.storage.list_decisions(tenant_id=self.tenant)), 1)


class SessionsTest(_Base):

    def test_an_unknown_session_is_404(self):
        for call in (lambda: self.client.get("{0}/nope".format(BASE), headers=self.as_()),
                     lambda: self.client.post("{0}/nope/step".format(BASE), headers=self.as_()),
                     lambda: self.client.post("{0}/nope/run".format(BASE), headers=self.as_())):
            self.assertEqual(call().status_code, 404)

    def test_an_unknown_policy_is_404(self):
        self.assertEqual(self.client.post(BASE, headers=self.as_(),
                                          json={"policy_id": "ghost", "payload": {}}).status_code, 404)

    def test_a_session_can_be_ended(self):
        sid = self.start()["session_id"]
        self.assertEqual(self.client.delete("{0}/{1}".format(BASE, sid), headers=self.as_()).status_code, 200)
        self.assertEqual(self.client.get("{0}/{1}".format(BASE, sid), headers=self.as_()).status_code, 404)

    def test_another_workspace_cannot_step_this_session(self):
        """Sessions live in one process dict, so without a tenant check any workspace could step
        another's execution and read its payload."""
        sid = self.start()["session_id"]
        other = self.storage.create_tenant("Other Co")["id"]
        other_key = self.storage.generate_api_key_for_tenant(other, role="owner")["plaintext"]
        for call in (lambda: self.client.get("{0}/{1}".format(BASE, sid), headers=self.as_(other_key)),
                     lambda: self.client.post("{0}/{1}/step".format(BASE, sid), headers=self.as_(other_key))):
            self.assertEqual(call().status_code, 404)

    def test_sessions_are_capped_so_a_forgotten_tab_cannot_grow_the_process(self):
        original = debugger_router.MAX_SESSIONS
        debugger_router.MAX_SESSIONS = 3
        try:
            for _ in range(6):
                self.start()
            self.assertLessEqual(len(debugger_router._SESSIONS), 3)
        finally:
            debugger_router.MAX_SESSIONS = original


class ExecutorStopTest(_Base):
    """The one field the whole feature rests on."""

    def test_the_stop_defaults_to_off_so_ordinary_runs_are_untouched(self):
        self.assertIsNone(PolicyExecutor.debug_stop_before)
        ctx = asyncio.run(PolicyExecutor(self.storage).execute(
            policy=self.policy, payload={"score": 50}, tenant_id=self.tenant,
            source="api", simulate=True))
        self.assertEqual(ctx.outcome, "approve")
        self.assertNotEqual(ctx.status, "paused")

    def test_a_stop_pauses_before_that_index(self):
        executor = PolicyExecutor(self.storage)
        executor.debug_stop_before = 1
        ctx = asyncio.run(executor.execute(
            policy=self.policy, payload={"score": 50}, tenant_id=self.tenant,
            source="api", simulate=True))
        self.assertEqual(ctx.status, "paused")
        self.assertEqual(ctx.paused_at_step, 1)
        self.assertEqual(len(ctx.step_trace), 1, "it ran past the stop")


if __name__ == "__main__":
    unittest.main()


class RbacAcrossAllFiveRolesTest(_Base):
    """The access each role should have, and no more (`docs/parity/LENS-QA.md`).

    `/api/v1/debug` is mapped to AUTHOR rather than DECIDE, and that is the interesting call.
    Stepping runs with simulate=True and writes nothing, so it is tempting to treat it as a read
    a `viewer` could do. It is not: the snapshot returns every payload field and every computed
    variable at every step of a PROD policy, which is strictly more than the decision surface
    exposes. It is the same reach an author already has by editing the policy and running the
    test console -- so, author.
    """

    ALL_ROLES = ("owner", "admin", "policy_maker", "reviewer", "viewer")
    STEPPERS = ("owner", "admin", "policy_maker")

    def key_for(self, role):
        return self.storage.generate_api_key_for_tenant(self.tenant, role=role)["plaintext"]

    def test_the_capability_is_mapped_EXPLICITLY_not_inherited_from_the_default(self):
        """Behaviourally identical to the fail-closed default landing on MANAGE_ACCESS would be a
        *stricter* accident; landing on AUTHOR by falling off the end of the map would be a looser
        one. Assert the choice so neither can happen silently."""
        from app import rbac

        self.assertIn("/api/v1/debug", rbac._AUTHOR_PREFIXES)
        self.assertEqual(rbac.required_capability("POST", "/api/v1/debug/sessions"), rbac.AUTHOR)

    def test_only_author_roles_can_start_a_session(self):
        for role in self.ALL_ROLES:
            with self.subTest(role=role):
                key = self.key if role == "owner" else self.key_for(role)
                status = self.client.post(
                    BASE, headers=self.as_(key),
                    json={"policy_id": "dbg_policy", "payload": {"score": 50}}).status_code
                expected = 200 if role in self.STEPPERS else 403
                self.assertEqual(status, expected,
                                 "{0} got {1} starting a debug session".format(role, status))

    def test_a_viewer_cannot_step_a_session_someone_else_started(self):
        """The escalation the capability choice exists to prevent: a read-only role walking a
        prod policy's execution and reading the applicant payload out of the snapshot."""
        snap = self.start()
        viewer = self.key_for("viewer")
        stepped = self.client.post("{0}/{1}/step".format(BASE, snap["session_id"]),
                                   headers=self.as_(viewer))
        self.assertEqual(stepped.status_code, 403, stepped.text)

    def test_a_reviewer_cannot_end_someone_elses_session(self):
        snap = self.start()
        ended = self.client.delete("{0}/{1}".format(BASE, snap["session_id"]),
                                   headers=self.as_(self.key_for("reviewer")))
        self.assertEqual(ended.status_code, 403, ended.text)


class AuditTest(_Base):
    """Stepping writes no Decision row, so the audit event is the ONLY record that a person
    walked a production policy's execution and read its payload."""

    def _events(self):
        return self.storage.list_audit_events(tenant_id=self.tenant)

    def test_starting_a_session_is_audited(self):
        before = len(self._events())
        snap = self.start()
        events = self._events()
        self.assertEqual(len(events), before + 1, "starting a debug session was not audited")
        event = [e for e in events if e.get("event_type") == "debug_session_started"]
        self.assertTrue(event, "no debug_session_started event")
        self.assertEqual(event[0]["entity_id"], snap["session_id"])

    def test_the_event_names_the_actor(self):
        """An audit line saying only 'a debug session started' answers nothing. The DoD asks for
        the ACTOR, and this is the assertion that a role key -- not the tenant default -- is what
        gets recorded."""
        key = self.storage.generate_api_key_for_tenant(self.tenant, role="policy_maker")
        self.client.post(BASE, headers=self.as_(key["plaintext"]),
                         json={"policy_id": "dbg_policy", "payload": {"score": 50}})
        event = [e for e in self._events() if e.get("event_type") == "debug_session_started"][0]
        metadata = event.get("metadata") or {}
        self.assertEqual(metadata.get("actor_role"), "policy_maker")
        self.assertEqual(metadata.get("actor_api_key_id"), key["id"])
        self.assertEqual(metadata.get("policy_id"), "dbg_policy")
