"""The fast path must DECLINE a policy it cannot execute, not silently skip the step.

FAST_DECIDE is meant to be a performance flag. Twice now it has been a correctness switch:

  * T1.2 -- fast_decide recorded a failed variable as an error event but still returned the
    fail-OPEN outcome, so turning the flag on undid the slow path's guard.
  * this -- `_IO_STEP_TYPES` listed only action/review_gate/transform/model, so a policy
    containing `workflow`, `loop`, `branch` or `monitor` was judged fast-servable. The compiled
    core does not implement those, so the step was silently skipped and the decision returned
    anyway.

The second one is sharp because of what a `workflow` step IS: a sub-policy that gates the
parent. A parent shaped [run the KYC child, outcome approve] returned APPROVE at HTTP 200 with
the child never executed. Agent 3-B fixed the slow path to fail closed on an unreachable child;
the fast path cannot fail closed on it, because it cannot run one at all -- so it has to decline
the policy and let the full executor take it.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app.executor import PolicyExecutor  # noqa: E402
from app.fast_decide import _IO_STEP_TYPES, is_fast_servable  # noqa: E402


def _policy(step_type: str) -> dict:
    return {"steps": [{"type": "rule", "ref_id": "r"}, {"type": step_type, "ref_id": "x"}]}


class FastPathDeclinesWhatItCannotRunTest(unittest.TestCase):
    CONTROL_FLOW = ("workflow", "loop", "branch", "monitor")

    def test_control_flow_steps_are_not_fast_servable(self):
        for step_type in self.CONTROL_FLOW:
            with self.subTest(step=step_type):
                self.assertFalse(
                    is_fast_servable(_policy(step_type)),
                    "a policy containing a {0} step is judged fast-servable, so the compiled "
                    "core silently skips it and returns a decision that never ran it".format(step_type))

    def test_a_sub_workflow_parent_is_never_fast_served(self):
        """The sharpest case, stated on its own: the skipped step is the gate."""
        self.assertFalse(is_fast_servable({
            "steps": [{"type": "workflow", "ref_id": "kyc_child"},
                      {"type": "outcome", "ref_id": "approve"}]
        }), "a parent whose gating child would be skipped was fast-served, returning approve")

    def test_pure_steps_are_still_fast_servable(self):
        """The regression guard -- declining everything would make the flag pointless."""
        for step_type in ("rule", "scorecard", "outcome", "decision_table"):
            with self.subTest(step=step_type):
                self.assertTrue(is_fast_servable(_policy(step_type)))

    def test_every_step_type_the_executor_dispatches_is_classified(self):
        """Anti-rot. The executor grew from 8 step types to 13; each time one is added, the fast
        path must either implement it or decline it. Landing in neither list is how this bug
        happened, so a new type now fails here rather than in production."""
        import inspect

        source = inspect.getsource(PolicyExecutor._execute_step_body)
        dispatched = {
            token.strip('"')
            for token in source.split()
            if token.startswith('"') and token.endswith('"') and token.strip('"').isidentifier()
        }
        # The types the compiled core genuinely implements.
        pure = {"rule", "scorecard", "outcome", "decision_table"}
        # `connector` only supplies payloads; it needs no execution on either path.
        benign = {"connector"}
        known = pure | benign | set(_IO_STEP_TYPES)
        executor_types = {t for t in dispatched if t in {
            "connector", "rule", "scorecard", "decision_table", "transform", "action", "model",
            "review_gate", "outcome", "loop", "branch", "workflow", "monitor"}}
        unclassified = sorted(executor_types - known)
        self.assertEqual(unclassified, [],
                         "step type(s) {0} are dispatched by the executor but are in neither the "
                         "fast path's servable set nor its decline set, so a policy using them "
                         "would be fast-served and the step silently skipped".format(unclassified))


if __name__ == "__main__":
    unittest.main()
