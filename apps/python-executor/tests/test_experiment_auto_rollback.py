"""A canary that breaches its guardrails is halted without a human (parity P11).

`champion_challenger.analyze_champion_challenger` has computed a promote / hold / ROLLBACK
recommendation for a while, and `routers/experiments.py` used it -- but only to REFUSE a
promotion someone asked for. Nothing acted on "rollback" on its own, so a challenger that started
rejecting everyone kept taking its share of live traffic until somebody opened the analytics
page. The row's e2eFlow is "promote at 5% canary -> watch the metric -> AUTO-ROLLBACK on SLO
breach"; the auto half was the missing word.

THE DISTINCTION THAT MATTERS, and the one most of these tests are about: it halts on a GUARDRAIL
BREACH and not on "the challenger is merely losing". A guardrail is an explicit statement by the
author that crossing this line is unacceptable. Underperforming on lift is a business judgement,
and `recommend_action` returns "rollback" for a negative lift too -- acting on that would stop
experiments for doing exactly what experiments are for.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import scheduler  # noqa: E402
from app.storage import Storage  # noqa: E402


def analysis(recommendation="rollback", breached=True, breaches=("approval rate below floor",),
             challenger_id="challenger"):
    return {
        "championChallenger": {
            "champion": {"id": "champion", "stats": {}},
            "challengers": [{
                "id": challenger_id,
                "recommendation": recommendation,
                "guardrails": {"breached": breached, "breaches": list(breaches)},
            }],
        }
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "rollback.db"))
        self.tenant = self.storage.default_tenant_id
        self._leader = scheduler._IS_LEADER
        scheduler._IS_LEADER = True

    def tearDown(self):
        scheduler._IS_LEADER = self._leader
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        self.tempdir.cleanup()

    def running_experiment(self, name="canary"):
        return self.storage.create_or_update_experiment({
            "id": "exp_{0}".format(name), "name": name, "status": "running",
            "target_policy_id": "pol_loan",
            "variants": [{"id": "champion", "role": "champion", "weight": 90},
                         {"id": "challenger", "role": "challenger", "weight": 10}],
        }, tenant_id=self.tenant)

    def run_sweep(self, report):
        with mock.patch("app.analytics.experiment_analytics", return_value=report):
            return scheduler.auto_rollback_experiments(self.storage)

    def status_of(self, experiment_id):
        return self.storage.get_experiment(experiment_id)["status"]


class ItHaltsOnAGuardrailBreachTest(_Base):

    def test_a_breaching_challenger_stops_the_experiment(self):
        experiment = self.running_experiment()
        result = self.run_sweep(analysis())
        self.assertEqual(self.status_of(experiment["id"]), "stopped")
        self.assertEqual(len(result["stopped"]), 1)
        self.assertEqual(result["stopped"][0]["breaches"], ["approval rate below floor"])

    def test_stopping_is_what_removes_it_from_live_traffic(self):
        """No separate teardown: resolve_experiment_assignment selects status == "running", so a
        stopped experiment stops receiving traffic on the very next decision and the champion
        serves 100% again because it is what remains."""
        from app.experiments import resolve_experiment_assignment

        experiment = self.running_experiment()
        before = resolve_experiment_assignment(self.storage, self.tenant, "pol_loan", "user-1")
        self.assertIsNotNone(before, "the fixture is not actually assigning traffic")
        self.run_sweep(analysis())
        after = resolve_experiment_assignment(self.storage, self.tenant, "pol_loan", "user-1")
        self.assertIsNone(after, "a stopped experiment is still assigning live traffic")
        _ = experiment

    def test_the_halt_is_audited_with_the_reason(self):
        """It changes what live traffic sees. An operator arriving later needs to know it
        happened, when, and on which guardrail -- and audit events feed the outbound webhook
        stream, so a subscriber can be told."""
        experiment = self.running_experiment()
        self.run_sweep(analysis(breaches=("reject rate above ceiling",)))
        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e.get("entity_id") == experiment["id"]]
        self.assertTrue(events, "the automatic halt was not audited")
        metadata = events[0].get("metadata") or events[0].get("metadata_json") or {}
        self.assertEqual(metadata.get("action"), "auto_rollback")
        self.assertIn("reject rate above ceiling", events[0]["detail"])


class ItDoesNotHaltForTheWrongReasonsTest(_Base):
    """The half that keeps this from being a footgun."""

    def test_a_losing_challenger_with_no_breach_is_left_alone(self):
        """recommend_action returns "rollback" for a negative lift too. Acting on that would stop
        experiments for doing exactly what experiments are for."""
        experiment = self.running_experiment()
        result = self.run_sweep(analysis(recommendation="rollback", breached=False, breaches=()))
        self.assertEqual(self.status_of(experiment["id"]), "running")
        self.assertEqual(result["stopped"], [])

    def test_a_hold_recommendation_is_left_alone(self):
        experiment = self.running_experiment()
        self.run_sweep(analysis(recommendation="hold"))
        self.assertEqual(self.status_of(experiment["id"]), "running")

    def test_a_promote_recommendation_is_left_alone(self):
        """Promotion stays a human decision -- this job only ever stops things."""
        experiment = self.running_experiment()
        self.run_sweep(analysis(recommendation="promote", breached=False, breaches=()))
        self.assertEqual(self.status_of(experiment["id"]), "running")

    def test_a_draft_experiment_is_never_touched(self):
        created = self.storage.create_or_update_experiment({
            "id": "exp_draft", "name": "draft", "status": "draft",
            "target_policy_id": "pol_loan", "variants": [],
        }, tenant_id=self.tenant)
        result = self.run_sweep(analysis())
        self.assertEqual(self.status_of(created["id"]), "draft")
        self.assertEqual(result["checked"], 0)

    def test_an_already_stopped_experiment_is_not_re_stopped(self):
        self.running_experiment()
        self.run_sweep(analysis())
        second = self.run_sweep(analysis())
        self.assertEqual(second["stopped"], [], "a stopped experiment was halted twice")


class ItIsSafeToRunOnAScheduleTest(_Base):

    def test_a_follower_replica_does_nothing(self):
        """N replicas each halting the same experiment would write the row N times and emit N
        alerts."""
        experiment = self.running_experiment()
        scheduler._IS_LEADER = False
        result = self.run_sweep(analysis())
        self.assertEqual(result, {"skipped": "not_leader"})
        self.assertEqual(self.status_of(experiment["id"]), "running")

    def test_one_tenants_failure_does_not_stop_the_sweep(self):
        """A sweep that aborts on the first bad tenant silently stops protecting every tenant
        after it in the list."""
        self.running_experiment()
        calls = {"n": 0}

        def flaky(storage, tenant_id, experiment_id):
            calls["n"] += 1
            raise RuntimeError("analytics is down")

        with mock.patch("app.analytics.experiment_analytics", side_effect=flaky):
            result = scheduler.auto_rollback_experiments(self.storage)
        self.assertEqual(result["stopped"], [])
        self.assertGreater(calls["n"], 0)

    def test_it_is_off_unless_explicitly_enabled(self):
        """An automated halt is the right behaviour and a surprising one to inherit silently on
        upgrade: a workspace mid-experiment would find it stopped by a job it never enabled."""
        from app import env

        self.assertFalse(env.env_bool("EXPERIMENT_AUTO_ROLLBACK", False))

    def test_no_running_experiments_is_a_cheap_no_op(self):
        result = self.run_sweep(analysis())
        self.assertEqual(result, {"checked": 0, "stopped": []})


if __name__ == "__main__":
    unittest.main()
