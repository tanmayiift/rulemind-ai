"""The canary ramp must move real traffic, not just the dashboard.

`app/champion_challenger.py` has had the ramp schedule, the guardrails and the
promote/rollback/hold recommendation for a while, and `routers/experiments.py` refuses to
promote a challenger the analysis does not recommend. One piece was missing and it was the
piece that matters: `current_ramp_weight` was called from `analytics.py` and nowhere else, so
`assign_variant` used the STATIC weight and a canary configured to ramp 10% -> 25% -> 50%
served 10% forever.

That failure mode is worse than an absent feature. An absent feature is visible. Here the
dashboard showed the challenger at 25% on day 3 while production sent it 10%, so every
guardrail reading an operator acted on was taken against a traffic split that did not exist.

These tests assert on the SPLIT OF ACTUAL ASSIGNMENTS over many users, not on the return value
of a helper -- the helper was already correct and already tested (tests/test_champion_challenger.py).
What was broken was that nothing called it.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.experiments import _effective_weight, _elapsed_days, assign_variant


def variants(ramp=None):
    challenger = {"id": "challenger", "role": "challenger", "weight": 10}
    if ramp is not None:
        challenger["ramp"] = ramp
    # Champion first, as the API writes them; assign_variant walks in order.
    return [{"id": "champion", "role": "champion", "weight": 90}, challenger]


RAMP = [{"day": 0, "weight": 10}, {"day": 3, "weight": 25}, {"day": 7, "weight": 50}]


def challenger_share(vs, elapsed_days=None, users=2000):
    """Fraction of users routed to the challenger. Deterministic: the hash is md5 of
    experiment:user, so this is a fixed number for a fixed user set, not a sample."""
    hits = 0
    for i in range(users):
        assigned = assign_variant("user-{0}".format(i), "exp-1", vs, elapsed_days=elapsed_days)
        if assigned and assigned["id"] == "challenger":
            hits += 1
    return hits / users


class TheRampMovesRealTrafficTest(unittest.TestCase):

    def test_the_share_grows_as_the_schedule_advances(self):
        day0 = challenger_share(variants(RAMP), elapsed_days=0)
        day3 = challenger_share(variants(RAMP), elapsed_days=3)
        day7 = challenger_share(variants(RAMP), elapsed_days=7)
        self.assertAlmostEqual(day0, 0.10, delta=0.03)
        self.assertAlmostEqual(day3, 0.25, delta=0.03)
        self.assertAlmostEqual(day7, 0.50, delta=0.03)
        self.assertLess(day0, day3, "the ramp did not move traffic between day 0 and day 3")
        self.assertLess(day3, day7)

    def test_without_the_fix_the_share_would_never_move(self):
        """The control: the same experiment, asked for an assignment with no elapsed time known,
        stays at the static weight. This is exactly what every caller got before."""
        static = challenger_share(variants(RAMP), elapsed_days=None)
        self.assertAlmostEqual(static, 0.10, delta=0.03)

    def test_a_ramp_step_holds_until_the_next_day_is_reached(self):
        """Day 6 is still on the day-3 step. A ramp that interpolated would fail here, and
        interpolating would be wrong -- the schedule is a set of discrete steps an operator
        signed off on, not a curve."""
        self.assertAlmostEqual(challenger_share(variants(RAMP), elapsed_days=6), 0.25, delta=0.03)

    def test_past_the_last_step_it_holds_rather_than_extrapolating(self):
        self.assertAlmostEqual(challenger_share(variants(RAMP), elapsed_days=90), 0.50, delta=0.03)

    def test_an_experiment_with_no_ramp_is_completely_unaffected(self):
        """Every experiment that exists today has no `ramp` key. None of them may shift."""
        for elapsed in (None, 0, 3, 7, 90):
            with self.subTest(elapsed=elapsed):
                self.assertAlmostEqual(
                    challenger_share(variants(), elapsed_days=elapsed), 0.10, delta=0.03)

    def test_a_legacy_split_that_does_not_total_100_is_not_silently_re_split(self):
        """The guard on the renormalisation, and the reason it is conditional.

        Renormalising the champion is only correct when a ramp is in play. Experiments already in
        the database were authored against the current behaviour and some do not total 100 --
        here 60/20, where assign_variant gives the champion 60%, the challenger 60-79, and the
        remaining 20% falls through to `variants[-1]`, which is also the challenger: 40% in all.
        Renormalising unconditionally would make the champion 80 and cut the challenger to 20,
        moving live traffic on an experiment nobody touched.
        """
        lopsided = [
            {"id": "champion", "role": "champion", "weight": 60},
            {"id": "challenger", "role": "challenger", "weight": 20},
        ]
        for elapsed in (None, 0, 3, 90):
            with self.subTest(elapsed=elapsed):
                self.assertAlmostEqual(
                    challenger_share(lopsided, elapsed_days=elapsed), 0.40, delta=0.03,
                    msg="an experiment with no ramp had its traffic split rewritten")

    def test_assignment_is_still_stable_for_a_given_user_within_a_step(self):
        """A user must not flip between champion and challenger on every request -- that would
        make the experiment unreadable and the user's experience incoherent."""
        first = assign_variant("user-42", "exp-1", variants(RAMP), elapsed_days=3)
        for _ in range(5):
            self.assertEqual(
                assign_variant("user-42", "exp-1", variants(RAMP), elapsed_days=3)["id"],
                first["id"])


class EffectiveWeightTest(unittest.TestCase):

    def test_it_falls_back_to_the_static_weight_with_no_ramp(self):
        self.assertEqual(_effective_weight({"weight": 10}, 5), 10)

    def test_it_falls_back_when_elapsed_time_is_unknown(self):
        """An experiment with a ramp but no start_date is a misconfiguration. Serving the static
        weight is the conservative reading -- the alternative is guessing which ramp step a
        canary is on, which is how a 5% canary silently becomes 50%."""
        self.assertEqual(_effective_weight({"weight": 10, "ramp": RAMP}, None), 10)

    def test_a_malformed_weight_does_not_crash_the_decision_path(self):
        for bad in ({"weight": "lots"}, {"weight": None}, {}):
            with self.subTest(bad=bad):
                self.assertEqual(_effective_weight(bad, None), 0)


class ElapsedDaysTest(unittest.TestCase):
    """storage.serialize_datetime emits naive UTC with a literal `Z`, which fromisoformat
    rejects before Python 3.11 -- and this backend supports 3.10."""

    def test_it_parses_the_format_storage_actually_emits(self):
        started = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0, tzinfo=None)
        self.assertAlmostEqual(_elapsed_days(started.isoformat() + "Z"), 3.0, delta=0.01)

    def test_it_accepts_a_datetime_object_too(self):
        self.assertAlmostEqual(
            _elapsed_days(datetime.now(timezone.utc) - timedelta(days=2)), 2.0, delta=0.01)

    def test_a_missing_or_unparseable_start_yields_none_not_an_exception(self):
        for value in (None, "", "   ", "not a date", 12345, {}):
            with self.subTest(value=value):
                self.assertIsNone(_elapsed_days(value))

    def test_a_future_start_date_clamps_to_zero(self):
        """Scheduling an experiment ahead must not produce a negative elapsed time, which
        current_ramp_weight would read as 'before the first step'."""
        future = datetime.now(timezone.utc) + timedelta(days=5)
        self.assertEqual(_elapsed_days(future), 0.0)


if __name__ == "__main__":
    unittest.main()
