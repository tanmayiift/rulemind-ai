"""On production traffic, the fast path must not invent data the executor refuses to invent.

THE DIVERGENCE. `PolicyExecutor._payloads_by_source` gates sample seeding:

    seed_samples = self._source in _SAMPLE_SEEDING_SOURCES   # {test_console, simulation, loop_debug}

with the comment saying why -- on production traffic an unsupplied field must STAY unsupplied, so
the variable that reads it fails and the decision fails closed, rather than quietly evaluating
against demo data. `api` is not in that set.

`fast_decide._compute_variables` had no such gate. It seeded every source from `sample_payload`
unconditionally. So /decide against the executor left the source empty and failed closed, while
/decide with FAST_DECIDE=1 filled it with the connector's sample -- which is a healthy, approving
example by construction. Same policy, same payload, opposite decision, selected by a performance
flag.

This is the third instance of that exact shape, after input validation and the evaluation budget
(both closed earlier the same day), and it is why all three are correctness bugs rather than
gaps: the fast path is what production runs, so a contract only the executor honours is not a
contract.

The batch path is the one caller that SHOULD seed, and it still does: `_run_decision_batch`
passes seed_samples=True on its fast branch and `source="simulation"` on its executor branch.
Those two must agree, and there is a test for that below rather than a comment about it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.fast_decide as fast_decide  # noqa: E402
import app.main as app_main  # noqa: E402
from app.executor import PolicyExecutor, _SAMPLE_SEEDING_SOURCES  # noqa: E402

SOURCE_ID = "seedsrc"
VARIABLE_ID = "seed_score"
SAMPLE_SCORE = 999          # the demo value: approving, and must never decide production traffic
VARIABLE_CODE = (
    '@variable(source="{0}")\n'
    "def {1}(payload, variables, apis):\n"
    "    return int(payload.get(\"score\") or 0)\n"
).format(SOURCE_ID, VARIABLE_ID)


class _Base(unittest.TestCase):
    def setUp(self):
        self.storage = app_main.storage
        self.tenant_id = self.storage.create_tenant("seed-{0}".format(time.time_ns()))["id"]
        self.storage.create_connector(
            {"id": SOURCE_ID, "name": "seed source", "schema_paths": ["score"],
             "sample_payload": {"score": SAMPLE_SCORE}, "is_active": True, "config": {}},
            tenant_id=self.tenant_id)
        self.storage.create_variable(
            {"id": VARIABLE_ID, "name": "score", "category": "risk", "source_id": SOURCE_ID,
             "code": VARIABLE_CODE, "status": "prod"},
            tenant_id=self.tenant_id)
        self.policy = {
            "id": "seed_policy", "name": "Seed", "status": "prod",
            "steps": [{"id": "s1", "type": "outcome", "ref_id": "approve"}],
            "defaultOutcome": "review",
        }
        fast_decide.invalidate(self.tenant_id)

    def tearDown(self):
        fast_decide.invalidate(self.tenant_id)

    def fast(self, payload=None, **kwargs):
        return fast_decide.fast_decide(
            self.storage, self.policy, payload or {}, self.tenant_id, log=False, **kwargs)

    def slow(self, payload=None, source="api"):
        return asyncio.run(PolicyExecutor(self.storage).execute(
            policy=self.policy, payload=payload or {}, tenant_id=self.tenant_id, source=source))


class ProductionTrafficIsNotSeededTest(_Base):

    def test_the_fast_path_does_not_invent_a_source_payload(self):
        decision = self.fast()
        self.assertNotEqual(
            decision["variables"][VARIABLE_ID], SAMPLE_SCORE,
            "a /decide call with no data for this source was answered with the demo sample")
        self.assertEqual(decision["variables"][VARIABLE_ID], 0)

    def test_both_paths_agree_on_an_unsupplied_source(self):
        """The parity assertion this file exists for."""
        self.assertEqual(
            self.fast()["variables"][VARIABLE_ID],
            self.slow()["variables"][VARIABLE_ID] if isinstance(self.slow(), dict)
            else self.slow().variables[VARIABLE_ID],
            "the fast path and the executor disagree about an unsupplied source")

    def test_a_supplied_payload_is_unaffected(self):
        """The control. Without it every assertion above passes against a build that ignores the
        caller's data entirely."""
        decision = self.fast({SOURCE_ID: {"score": 42}})
        self.assertEqual(decision["variables"][VARIABLE_ID], 42)

    def test_a_flat_field_still_reaches_the_source(self):
        """Flat top-level fields override a source's fields -- that behaviour predates this and
        must survive the seeding change, since with no sample there are no keys to override."""
        decision = self.fast({"score": 77})
        self.assertEqual(decision["variables"][VARIABLE_ID], 77)


class AuthorSurfacesStillSeedTest(_Base):

    def test_the_batch_path_opts_in(self):
        decision = self.fast(seed_samples=True)
        self.assertEqual(
            decision["variables"][VARIABLE_ID], SAMPLE_SCORE,
            "a backtest lost its sample pre-fill, so every historical replay reads empty")

    def test_the_executor_surface_it_matches_really_is_a_seeding_source(self):
        """Pins the pairing rather than trusting the comment: _run_decision_batch passes
        seed_samples=True on the fast branch and source="simulation" on the executor branch, so
        "simulation" has to be in _SAMPLE_SEEDING_SOURCES or the two branches of one endpoint
        disagree."""
        self.assertIn("simulation", _SAMPLE_SEEDING_SOURCES)

    def test_api_is_not_a_seeding_source(self):
        """The other half of the same pairing: /decide's executor branch passes source="api"."""
        self.assertNotIn("api", _SAMPLE_SEEDING_SOURCES)

    def test_the_author_surfaces_agree_across_both_paths(self):
        fast_seeded = self.fast(seed_samples=True)["variables"][VARIABLE_ID]
        slow_seeded = self.slow(source="simulation")
        slow_value = slow_seeded["variables"][VARIABLE_ID] if isinstance(slow_seeded, dict) \
            else slow_seeded.variables[VARIABLE_ID]
        self.assertEqual(fast_seeded, slow_value)
        self.assertEqual(fast_seeded, SAMPLE_SCORE)


if __name__ == "__main__":
    unittest.main()
