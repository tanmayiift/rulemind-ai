"""The soak's arrival-rate shape, which decides whether a month is calibratable.

`scripts/soak_traffic.py` had no tests, and three weeks of the calibration run were wasted
because of what it did: a sine between 0.4 and 4 rps against a system that flattens at ~32 rps
produced 3,860 sampled minutes whose per-minute p95 spanned 47.9ms to 48.0ms.
`calibrate_thresholds.py` correctly refused it, which is how it was noticed at all.

What this pins:

* the CONTENTION WINDOW exists and reaches its configured rate, because without it the
  distribution is flat and the month produces nothing;
* it wraps past midnight, because `13:00 + 3h` and `23:00 + 3h` are different kinds of range;
* the BURST does not compound with the window. Applying the burst last multiplied it -- a 6x
  burst inside a 40 rps window asked for 240 rps on a 32 rps system, which produces a queue and
  a timeout rather than a measurement. The burst hour is random, so this would have landed
  inside the window about one day in twelve and quietly produced an unusable outlier each time;
* the window is OPTIONAL, so the old flat shape is still reachable for anyone who wants it.
"""
from __future__ import annotations

import importlib.util
import os
import unittest


def _repo_root() -> str:
    here = os.path.abspath(os.path.dirname(__file__))
    while here != os.path.dirname(here):
        if os.path.exists(os.path.join(here, "docker-compose.yml")):
            return here
        here = os.path.dirname(here)
    raise RuntimeError("could not locate the repository root")


REPO = _repo_root()
_spec = importlib.util.spec_from_file_location(
    "soak_traffic", os.path.join(REPO, "scripts", "soak_traffic.py"))
soak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soak)

LO, HI = 0.4, 4.0
NO_BURST = -1          # an hour that never matches, so burst logic stays out of the way
PEAK, START, HOURS = 40.0, 13, 2.0


def rate(hour, burst_hour=NO_BURST, burst=6.0, peak=PEAK, start=START, hours=HOURS):
    return soak.rate_at(hour * 3600, LO, HI, burst_hour, burst, peak, start, hours)


class ContentionWindowTest(unittest.TestCase):
    def test_the_window_reaches_its_configured_rate(self):
        self.assertAlmostEqual(rate(13), PEAK)
        self.assertAlmostEqual(rate(14), PEAK)

    def test_outside_the_window_the_sine_is_unchanged(self):
        for hour in (0, 4, 8, 12, 15, 20):
            self.assertLessEqual(rate(hour), HI + 1e-9, hour)

    def test_the_trough_is_still_the_trough(self):
        """A flat-bottomed day is as useless as a flat-topped one: the quantiles need both."""
        self.assertLess(rate(4), 1.0)

    def test_the_window_wraps_past_midnight(self):
        for hour in (23, 0, 1):
            self.assertAlmostEqual(rate(hour, start=23, hours=3), PEAK, msg=str(hour))
        for hour in (22, 2, 3):
            self.assertLess(rate(hour, start=23, hours=3), HI + 1e-9, hour)

    def test_zero_peak_disables_it(self):
        self.assertLess(rate(13, peak=0.0), HI + 1e-9)


class BurstDoesNotCompoundTest(unittest.TestCase):
    def test_a_burst_inside_the_window_does_not_multiply_it(self):
        """40 x 6 = 240 rps against a ~32 rps ceiling is a queue, not a measurement."""
        self.assertAlmostEqual(rate(13, burst_hour=13), PEAK)

    def test_a_burst_outside_the_window_still_bursts(self):
        """It has a job -- exercising the queue near the ceiling -- and must keep doing it."""
        self.assertAlmostEqual(rate(16, burst_hour=16), HI * 6.0)
        self.assertGreater(rate(16, burst_hour=16), rate(16))

    def test_the_rate_never_goes_to_zero(self):
        """expovariate(0) raises; a zero rate would kill the generator mid-month."""
        for hour in range(24):
            self.assertGreater(rate(hour, peak=0.0), 0.0, hour)


class ReportingContractTest(unittest.TestCase):
    """The generator must be able to say it is behind. It could not, and that is why nobody
    noticed it was delivering 22 rps of a requested 60."""

    def test_it_accepts_a_worker_count(self):
        import argparse
        import contextlib
        import io as _io

        parser = None
        src = open(os.path.join(REPO, "scripts", "soak_traffic.py"), encoding="utf-8").read()
        self.assertIn("--workers", src,
                      "a single blocking request per loop caps the rate at 1/latency")
        self.assertIn("achieved", src, "the report must carry the ACHIEVED rate, not the target")
        self.assertIn("limited", src, "429s must be counted apart from decisions")
        del parser, argparse, contextlib, _io

    def test_rejections_are_excluded_from_the_latency_sample(self):
        """A 429 returns in ~8ms against ~195ms for a decision, so mixing them makes an
        overloaded, mostly-rejected soak report a beautiful p50."""
        src = open(os.path.join(REPO, "scripts", "soak_traffic.py"), encoding="utf-8").read()
        self.assertIn('if status == 429:', src)
        self.assertRegex(src, r'if status == 429:\s*\n\s*stats\["limited"\] \+= 1')


if __name__ == "__main__":
    unittest.main()
