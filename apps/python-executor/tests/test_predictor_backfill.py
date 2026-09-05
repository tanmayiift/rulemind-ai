"""Backfilling a predictor from historical decisions (parity P12's "backfill it").

WHY THIS IS THE WHOLE OF P12. The row read "No feature factory: 0 hits. Variables are authored one
at a time in Python; there is no derived-feature pipeline, no backfill, and no feature store",
sized multi-month. Two of its three e2e clauses were already true and are asserted below so the
claim is checked rather than argued:

  * "define a derived feature" -- `executor._compute_variables` calls
    `sandbox.execute_variable(code, source_payload, values)` where `values` is the accumulated
    map of already-computed variables, so a variable that reads other variables IS a derived
    feature. `test_a_variable_can_be_derived_from_other_variables` runs one.
  * "use it in a scorecard" -- scorecards bin on `variable_id`, which is the ordinary path.

Only backfill was missing, and it was missing in a way that hurt the SHIPPED P20 feature too:
`predictors.observe` runs on the decision path, so a predictor created today has no history, and
with a 30-day default baseline its drift report has nothing to compare against for a month.

THE TIMESTAMP IS THE HARD PART, and most of these tests are about it. `PredictorValue.created_at`
defaults to now. A backfill that accepts that default drops every replayed value into the current
window, so the drift report compares today against today and confidently reports NO DRIFT. That is
worse than an empty baseline, because an empty one is visibly empty.
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
from app import predictors  # noqa: E402
from app.storage import Storage  # noqa: E402

BASE = "/api/v1/predictors"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "pb.db"))
        self.storage = app_main.storage
        self.client = TestClient(app_main.app)
        self.tenant = self.storage.default_tenant_id
        self.key = self.storage.default_api_key or ""
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        self.client.close()
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        app_main.storage = self._orig
        self.tempdir.cleanup()

    def as_(self, key=None):
        return {"x-api-key": key or self.key}

    def key_for(self, role):
        return self.storage.generate_api_key_for_tenant(self.tenant, role=role)["plaintext"]

    def make_predictor(self, **overrides):
        payload = {"name": "score", "source_type": "variable", "source_id": "risk_score"}
        payload.update(overrides)
        response = self.client.post(BASE, headers=self.as_(), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def make_decisions(self, values, days_ago_start=40):
        """Historical decisions, oldest first, one per day going back.

        The timestamp is written by UPDATING the row afterwards, not by passing `created_at` to
        `add_decision` -- which ignores it and stamps now. That is correct and worth not
        "fixing": a caller able to backdate a decision could reorder the audit chronology the
        product exists to be trusted about. Backdating is a test concern, so it happens here.
        """
        from app.models import Decision

        made = []
        for offset, value in enumerate(values):
            decision = self.storage.add_decision({
                "policy_id": "p1", "outcome": "approve",
                "payload": {"x": 1},
                "computed_variables": {"risk_score": value},
            }, tenant_id=self.tenant)
            when = datetime.utcnow() - timedelta(days=days_ago_start - offset)
            with self.storage.connect() as session:
                row = session.get(Decision, decision["id"])
                row.created_at = when
            made.append(decision)
        return made

    def backfill(self, predictor_id, key=None, **body):
        return self.client.post("{0}/{1}/backfill".format(BASE, predictor_id),
                                headers=self.as_(key), json=body or {})


class TheOtherTwoClausesAlreadyWorkedTest(unittest.TestCase):
    """Checked rather than argued, because the parity row asserted the opposite."""

    def test_a_variable_can_be_derived_from_other_variables(self):
        """`execute_variable(code, payload, variables_map)` -- the third argument is the map of
        already-computed variables, so a derived feature is an ordinary variable that reads it.
        That is P12's "define a derived feature", and it predates this work."""
        os.environ["SANDBOX_MODE"] = "inline"
        try:
            from app.sandbox import execute_variable

            code = ('@variable(source="custom")\n'
                    "def dti(payload, variables, apis):\n"
                    '    return round(variables.get("total_emi", 0) / max(variables.get("income", 1), 1), 3)\n')
            result = execute_variable(code, {}, {"total_emi": 25000, "income": 100000})
            self.assertIsNone(result.get("error"), result)
            self.assertEqual(result["value"], 0.25)
        finally:
            os.environ.pop("SANDBOX_MODE", None)


class BackfillTest(_Base):
    def test_it_records_a_value_per_historical_decision(self):
        self.make_decisions([700, 710, 720])
        predictor = self.make_predictor()

        response = self.backfill(predictor["id"])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["written"], 3)
        self.assertEqual(result["scanned"], 3)

    def test_values_carry_the_DECISION_time_not_now(self):
        """The point of the whole exercise.

        With `created_at` left to its default, all three land in today's window, the baseline
        window is empty, and the drift report compares today against today -- reporting no drift
        with total confidence. A wrong baseline is worse than an empty one.
        """
        self.make_decisions([700, 710, 720], days_ago_start=40)
        predictor = self.make_predictor()
        self.backfill(predictor["id"])

        now = datetime.utcnow()
        old_window = self.storage.predictor_values_between(
            self.tenant, predictor["id"], now - timedelta(days=45), now - timedelta(days=30))
        self.assertEqual(len(old_window), 3,
                         "backfilled values were not stamped with the decision's own time")
        recent = self.storage.predictor_values_between(
            self.tenant, predictor["id"], now - timedelta(days=1), now + timedelta(days=1))
        self.assertEqual(recent, [], "backfilled history landed in the current window")

    def test_running_it_twice_does_not_double_the_series(self):
        """There is no unique constraint on (predictor_id, decision_id) -- the decision path
        writes each exactly once so it never needed one. A doubled series does not fail loudly;
        it halves the apparent drift."""
        self.make_decisions([700, 710, 720])
        predictor = self.make_predictor()

        first = self.backfill(predictor["id"]).json()
        second = self.backfill(predictor["id"]).json()
        self.assertEqual(first["written"], 3)
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["skipped_already_recorded"], 3)

        now = datetime.utcnow()
        everything = self.storage.predictor_values_between(
            self.tenant, predictor["id"], now - timedelta(days=365), now + timedelta(days=1))
        self.assertEqual(len(everything), 3, "the second run doubled the series")

    def test_a_decision_without_the_number_is_skipped_not_zeroed(self):
        """A gap in a monitoring series is honest; a fabricated 0 moves the distribution."""
        self.make_decisions([700])
        self.storage.add_decision({"policy_id": "p1", "outcome": "approve", "payload": {},
                                   "computed_variables": {}}, tenant_id=self.tenant)
        predictor = self.make_predictor()

        result = self.backfill(predictor["id"]).json()
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["skipped_no_value"], 1)

    def test_it_is_bounded_per_call_and_says_when_more_remains(self):
        self.make_decisions([700 + i for i in range(6)])
        predictor = self.make_predictor()

        page = self.backfill(predictor["id"], limit=3).json()
        self.assertEqual(page["scanned"], 3)
        self.assertTrue(page["more_available"])

    def test_the_cap_holds_even_when_a_larger_limit_is_asked_for(self):
        """One request must not become an unbounded scan plus an unbounded insert.

        The cap is lowered for the test and there are MORE decisions than it, because the obvious
        version -- one decision, `assertLessEqual(scanned, MAX)` -- holds whether the cap is
        applied or not. It did, and the mutation that removed the cap survived it.
        """
        self.make_decisions([700 + i for i in range(5)])
        predictor = self.make_predictor()
        original = predictors.MAX_BACKFILL_DECISIONS
        predictors.MAX_BACKFILL_DECISIONS = 2
        try:
            result = self.backfill(predictor["id"], limit=10_000_000).json()
        finally:
            predictors.MAX_BACKFILL_DECISIONS = original
        self.assertEqual(result["scanned"], 2,
                         "the requested limit overrode the cap")
        self.assertEqual(result["written"], 2)

    def test_backfilled_history_makes_the_drift_report_answerable(self):
        """The reason this exists at all: before it, a predictor created today had an empty
        baseline for 30 days, on a feature whose entire job is comparison."""
        # The windows are ADJACENT: baseline is [now-37d, now-7d), current is [now-7d, now).
        # 40 days ago is outside the baseline, not inside it -- placing the fixture there was how
        # this test first failed, and the endpoint was right.
        self.make_decisions([700, 705, 710, 715], days_ago_start=30)   # baseline window
        self.make_decisions([600, 605, 610, 615], days_ago_start=5)    # current window
        predictor = self.make_predictor()

        before = self.client.get("{0}/{1}/drift".format(BASE, predictor["id"]),
                                 headers=self.as_()).json()
        self.backfill(predictor["id"])
        after = self.client.get("{0}/{1}/drift".format(BASE, predictor["id"]),
                                headers=self.as_()).json()

        self.assertEqual(before.get("baseline_count", 0), 0,
                         "the predictor had a baseline before anything was backfilled")
        self.assertGreater(after.get("baseline_count", 0), 0,
                           "backfill did not give the drift report a baseline")
        self.assertGreater(after.get("current_count", 0), 0)

    def test_an_unknown_predictor_is_404(self):
        self.assertEqual(self.backfill("nope").status_code, 404)


class RbacAndAuditTest(_Base):
    ALL_ROLES = ("owner", "admin", "policy_maker", "reviewer", "viewer")
    AUTHORS = ("owner", "admin", "policy_maker")

    def test_only_author_roles_can_backfill(self):
        """A backfill writes to the monitoring series every drift report reads, so it is an
        authoring action rather than a read."""
        self.make_decisions([700])
        predictor = self.make_predictor()
        for role in self.ALL_ROLES:
            with self.subTest(role=role):
                key = self.key if role == "owner" else self.key_for(role)
                status = self.backfill(predictor["id"], key=key).status_code
                self.assertEqual(status, 200 if role in self.AUTHORS else 403,
                                 "{0} got {1} backfilling".format(role, status))

    def test_another_workspace_cannot_backfill_this_predictor(self):
        self.make_decisions([700])
        predictor = self.make_predictor()
        other = self.storage.create_tenant("Other")
        other_key = self.storage.generate_api_key_for_tenant(other["id"], role="admin")["plaintext"]
        self.assertEqual(self.backfill(predictor["id"], key=other_key).status_code, 404)

    def test_the_backfill_is_audited_with_the_actor(self):
        self.make_decisions([700, 710])
        key = self.storage.generate_api_key_for_tenant(self.tenant, role="policy_maker")
        predictor = self.make_predictor()
        self.backfill(predictor["id"], key=key["plaintext"])

        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e.get("event_type") == "predictor_backfilled"]
        self.assertEqual(len(events), 1, "the backfill was not audited")
        metadata = events[0].get("metadata") or {}
        self.assertEqual(metadata.get("actor_role"), "policy_maker")
        self.assertEqual(metadata.get("actor_api_key_id"), key["id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
