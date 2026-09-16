"""The `model` and `monitor` step types, one real execution test each.

`monitor` already had a genuine test (test_workflow_engine.py:183 asserts a `monitor` trace
entry with `alerted` and `scheduled` true), so the test here targets what that one cannot see:
that the step is a POST-decision side effect and never alters the outcome.

`model` is the step type that LOOKS covered and is not. Its only test in the repo
(test_edge_cases.py:511) asserts `"model" in str(body) or "risk_score" in str(body)` against a
policy-execute response -- and `"model"` is in `str(body)` merely because the step dict is
echoed back into the trace. Worse, a `model` step that RAISES is not in `_run_steps`' fail-closed
gate set (executor.py:846 -- rule / scorecard / decision_table / workflow), so the request still
returns 200 and that assertion still passes. The existing test cannot distinguish a working
model from a completely broken one.
"""
from __future__ import annotations

import asyncio
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.fast_decide as fast_decide_module  # noqa: E402
import app.main as app_main  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.storage import Storage  # noqa: E402


class SummingModel:
    """Predicts the sum of its features.

    Module-level so the spawned model-pool worker can unpickle it by importing this module --
    the same arrangement test_edge_cases.py uses. A sum is chosen over a constant deliberately:
    a constant prediction would still be produced if `inputMapping` silently resolved every
    feature to None, and this test exists to prove the mapping actually carried values.
    """

    def predict(self, rows):
        return [sum(row) for row in rows]


_MODEL_B64 = base64.b64encode(pickle.dumps(SummingModel())).decode()

PAYLOAD = {"bureau": {"scores": [{"scoreName": "CIBILTUSC3", "score": "00741"}]},
           "loan": {"dti_ratio": 0.3}}


class ModelAndMonitorStepTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "stepsweep.db"))
        self.storage = app_main.storage
        self.tenant_id = self.storage.default_tenant_id
        fast_decide_module.invalidate()

    def tearDown(self):
        fast_decide_module.invalidate()
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        app_main.storage = self._orig_storage

    def _run(self, steps, policy_id):
        policy = {"id": policy_id, "name": policy_id, "defaultOutcome": "review", "steps": steps}
        self.storage.create_policy(dict(policy), tenant_id=self.tenant_id)
        return asyncio.run(PolicyExecutor(self.storage).execute(
            policy=policy, payload=PAYLOAD, tenant_id=self.tenant_id,
            source="api", simulate=False))

    def _create_model(self):
        model = self.storage.create_model(
            {"id": "m_sum", "name": "Summing Model",
             "model_blob": base64.b64decode(_MODEL_B64)},
            tenant_id=self.tenant_id)
        return model["id"]

    # ----------------------------------------------------------------------- model

    def test_a_model_step_predicts_from_its_input_mapping_and_publishes_the_result(self):
        """The assertion the existing model test does not make: the PREDICTION VALUE.

        Three things at once, each of which the `"model" in str(body)` assertion misses:
          * `config.inputMapping` resolved real values (the sum proves it -- an all-None
            mapping would error or sum to 0, not to 12 + 30);
          * the prediction is written back into `ctx.variables` under `config.outputVariable`,
            which is how a downstream rule reads a model score at all;
          * the step result carries the prediction, so the trace can explain the decision.
        """
        model_id = self._create_model()
        ctx = self._run([
            {"type": "model", "id": "mdl", "ref_id": model_id, "config": {
                "inputMapping": {"a": 12, "b": 30},
                "features": ["a", "b"],
                "outputVariable": "risk_score",
            }},
            {"type": "outcome", "ref_id": "approve"},
        ], "p_model")

        entry = next(e for e in ctx.step_trace if e["step"].get("id") == "mdl")
        self.assertIsNone(entry["error"], "the model step raised: {0}".format(entry["error"]))
        self.assertEqual(entry["result"]["prediction"], 42,
                         "the model did not receive its mapped features")
        self.assertEqual(ctx.variables["risk_score"], 42,
                         "the prediction was not published to ctx.variables, so no downstream "
                         "rule could read it")
        self.assertEqual(ctx.outcome, "approve")

    def test_DEFECT_a_model_step_that_cannot_run_does_not_fail_closed(self):
        """A model step naming a model that does not exist is SILENTLY SKIPPED.

        `_execute_model` raises `ValueError("Unknown model: ...")`, and its own code comment
        (executor.py:2016-2019) states the intent plainly -- inventing a prediction "or silently
        skipping the step -- would let the policy decide on a value no model gave". But
        `_run_steps` only escalates to review for rule / scorecard / decision_table / workflow
        (executor.py:846). `model` is not in that set, so the exception is swallowed into the
        trace and the run continues.

        Failure scenario: a policy is [model -> risk_score, rule: reject if risk_score > 0.8,
        outcome approve]. The model id is stale after a migration. `risk_score` is never set,
        logic.py answers False for every comparison against a missing value, the reject rule does
        not fire, and the policy APPROVES at HTTP 200 on a score no model produced. That is the
        fail-open shape `_fail_closed_on_variable_errors` exists to prevent, reached by a route
        that guard does not cover.

        This test asserts TODAY'S behaviour so the gap is recorded and attributable rather than
        invisible. The fix is to add "model" to the gate-set tuple at executor.py:846 -- it is a
        one-word change, but it converts approvals into reviews for any tenant with a broken
        model step, so it is a deliberate decision for the maintainers rather than a drive-by
        edit from an audit.
        """
        ctx = self._run([
            {"type": "model", "id": "mdl", "ref_id": "model_that_does_not_exist", "config": {
                "inputMapping": {"a": 1}, "outputVariable": "risk_score"}},
            {"type": "outcome", "ref_id": "approve"},
        ], "p_model_missing")

        entry = next(e for e in ctx.step_trace if e["step"].get("id") == "mdl")
        self.assertIn("Unknown model", entry["error"] or "")
        self.assertNotIn("risk_score", ctx.variables,
                         "no prediction should have been invented")
        # The two assertions that record the defect:
        self.assertEqual(ctx.outcome, "approve",
                         "if this is no longer 'approve', the model step now fails closed -- "
                         "delete this test and move the assertion to the happy path")
        self.assertEqual(ctx.status, "completed")

    # --------------------------------------------------------------------- monitor

    def test_a_monitor_step_schedules_reevaluation_without_touching_the_outcome(self):
        """A monitor is a post-decision side effect, so it must be outcome-neutral.

        `reevaluateInDays` is asserted through the AUDIT EVENT rather than the step result,
        because the audit row is the part anything downstream actually consumes -- the step
        result is discarded once the trace is written. `alertUrl` is deliberately omitted: it
        would make the test depend on an outbound HTTP call, and `_execute_monitor` swallows
        alert failures by design (`alertError`), so a network-dependent assertion there would be
        flaky in exactly the direction that hides a regression.
        """
        before = self._run([{"type": "outcome", "ref_id": "reject"}], "p_no_monitor")
        ctx = self._run([
            {"type": "outcome", "ref_id": "reject"},
            {"type": "monitor", "id": "mon", "name": "drift watch",
             "config": {"reevaluateInDays": 30}},
        ], "p_monitor")

        entry = next(e for e in ctx.step_trace if e["step"].get("id") == "mon")
        self.assertIsNone(entry["error"])
        self.assertTrue(entry["result"]["scheduled"])
        self.assertEqual(entry["result"]["reevaluateInDays"], 30)
        self.assertEqual(entry["result"]["monitor"], "drift watch")

        self.assertEqual(ctx.outcome, before.outcome, "the monitor step changed the outcome")
        self.assertEqual(ctx.outcome, "reject")

        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant_id)
                  if e.get("event_type") == "monitor_scheduled"
                  and (e.get("metadata") or {}).get("policy_id") == "p_monitor"]
        self.assertEqual(len(events), 1, "the re-evaluation was not recorded in the audit log")
        self.assertEqual(events[0]["metadata"]["reevaluateInDays"], 30)
        self.assertEqual(events[0]["entity_id"], ctx.execution_id)


if __name__ == "__main__":
    unittest.main()
