"""The load test must not report a slow-path number as a fast-path one.

`qa/loadtest.py` defaulted `RM_POLICY` to `policy_instant_personal_loan`. That policy's steps
are `action, connector, outcome, review_gate, rule, scorecard`, and `action` and `review_gate`
are both in `_IO_STEP_TYPES` -- so it is PERMANENTLY ineligible for the fast path. And
`FAST_DECIDE=1` does not error on an ineligible policy: `fast_path_eligible` returns False and
the request falls through to the full executor, silently, with a 200.

So every fast-path number this repository has ever produced through its own load test was a
slow-path number, produced with the flag on and the flag doing nothing. That is the same
failure as a test that passes without asserting -- the harness reported success at the thing it
was not doing -- and it is why `README.md` carried a throughput tier no shipped policy could
reach.

These tests pin the harness's guard and, more importantly, pin the LIST it guards with against
the server's own, because a copy that drifts is a guard that stops guarding.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
APP_ROOT = REPO / "apps" / "python-executor"
for path in (str(APP_ROOT), str(REPO / "qa")):
    if path not in sys.path:
        sys.path.insert(0, path)

import loadtest  # noqa: E402  (qa/loadtest.py)
from app.fast_decide import _IO_STEP_TYPES  # noqa: E402


class _FakePolicyClient:
    """Stands in for httpx so this runs without a server. The harness talks HTTP on purpose --
    it must not import the process it is measuring -- so the seam is the client."""

    def __init__(self, steps):
        self._steps = steps

    def get(self, _path):
        steps, outer = self._steps, self

        class _Response:
            def raise_for_status(self):
                if steps is None:
                    raise RuntimeError("no such policy")

            def json(self):
                return {"steps": outer._steps}

        return _Response()


class LoadTestEligibilityTests(unittest.TestCase):
    def test_the_harness_list_matches_the_servers(self):
        """The load test duplicates `_IO_STEP_TYPES` because it cannot import the server. A
        duplicate that drifts silently stops excluding a type, and the harness goes back to
        measuring the other branch -- so the copies are pinned to each other here, in the one
        place that can see both."""
        self.assertEqual(loadtest._IO_STEP_TYPES, _IO_STEP_TYPES)

    def test_the_loadtest_default_policy_is_still_the_ineligible_one(self):
        """Not a recommendation -- a record. If someone changes the default to an eligible
        policy this fails and they can delete the test; what must not happen is the default
        quietly staying ineligible while the guard is removed."""
        self.assertEqual(loadtest.POLICY, "policy_instant_personal_loan")

    def test_a_policy_with_a_review_gate_is_reported_ineligible(self):
        eligible, reason = loadtest.fast_path_shape(
            _FakePolicyClient([{"type": "rule"}, {"type": "review_gate"}, {"type": "outcome"}]),
            "p")
        self.assertIs(eligible, False)
        self.assertIn("review_gate", reason)

    def test_a_policy_with_an_action_is_reported_ineligible(self):
        eligible, reason = loadtest.fast_path_shape(
            _FakePolicyClient([{"type": "action"}, {"type": "outcome"}]), "p")
        self.assertIs(eligible, False)
        self.assertIn("action", reason)

    def test_a_pure_compute_policy_is_reported_eligible(self):
        eligible, _ = loadtest.fast_path_shape(
            _FakePolicyClient([{"type": "rule"}, {"type": "scorecard"}, {"type": "outcome"}]),
            "p")
        self.assertIs(eligible, True)

    def test_an_unreadable_policy_is_UNKNOWN_rather_than_eligible(self):
        """Fail-unknown, not fail-eligible. A probe that cannot read the policy must not answer
        'yes' -- that is how the original defect would come back through the error path."""
        eligible, reason = loadtest.fast_path_shape(_FakePolicyClient(None), "p")
        self.assertIsNone(eligible)
        self.assertIn("could not read", reason)

    def test_every_io_step_type_is_caught_individually(self):
        for step_type in sorted(_IO_STEP_TYPES):
            with self.subTest(step_type=step_type):
                eligible, _ = loadtest.fast_path_shape(
                    _FakePolicyClient([{"type": step_type}, {"type": "outcome"}]), "p")
                self.assertIs(eligible, False, f"'{step_type}' was not caught")


class ReadmeRetractionTests(unittest.TestCase):
    def test_the_readme_no_longer_advertises_the_rust_tier(self):
        """`README.md` advertised `FAST_DECIDE=1` + Rust core at "high hundreds to low
        thousands/s". Zero of four seeded policies could reach that configuration, and the Rust
        serving branch has since been removed outright. A buyer who benchmarks you is the wrong
        person to discover that."""
        text = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("| Rules-only (lean) | `FAST_DECIDE=1` + Rust core |", text)
        self.assertIn("one of four seeded policies is fast-path eligible", text)


if __name__ == "__main__":
    unittest.main()
