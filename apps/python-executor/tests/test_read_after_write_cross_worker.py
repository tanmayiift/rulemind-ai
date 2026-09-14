"""The decision_id /decide returns 404s when the follow-up GET lands on another worker (5.6).

``decision_log.flush()`` waits on ``_pending``, a set of ``concurrent.futures.Future`` objects
created *in this process*. Its docstring promised "so a subsequent read sees them", and inside
one process that is true.

Under ``--workers 4`` it is not. The POST /decide that computed the decision and the GET that
reads it back are routed independently, so roughly three times in four the reading worker's
``_pending`` is empty: ``flush()`` returns instantly, having waited on nothing, and the read
misses a row the *other* worker's background pool has not committed yet. The API hands the
caller a ``decision_id`` and then 404s on it. Worse than a plain race, the docstring asserted
the opposite, so anyone reading the code concluded the case was handled.

A true cross-worker barrier is not cheap -- it needs every worker's in-flight write ids in
shared state, consulted on every read, which costs more than the async write saves. So the
read path tolerates the gap instead: flush what this process owes, then re-poll for a short
bounded window. All of the cost lands on the miss path; a row already committed returns on
the first fetch with no sleep at all.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

import app.decision_log as decision_log  # noqa: E402


class CrossWorkerReadAfterWriteTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("DECISION_READ_AFTER_WRITE_MS")
        os.environ["DECISION_READ_AFTER_WRITE_MS"] = "800"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DECISION_READ_AFTER_WRITE_MS", None)
        else:
            os.environ["DECISION_READ_AFTER_WRITE_MS"] = self._saved

    def test_a_row_written_by_another_worker_is_waited_for(self):
        """The whole bug: flush() has nothing to wait on, so the read has to."""
        state = {"row": None}

        def other_worker():
            time.sleep(0.15)  # the sibling's background pool commits, after our read starts
            state["row"] = {"id": "d1"}

        threading.Thread(target=other_worker, daemon=True).start()
        result = decision_log.await_row(lambda: state["row"])
        self.assertEqual(result, {"id": "d1"},
                         "the read gave up before the other worker's write landed -- this is the "
                         "404 on a decision_id the API just returned")

    def test_a_committed_row_costs_no_wait_at_all(self):
        """The tolerance must not become a latency tax on the 99.9% that hit."""
        started = time.monotonic()
        result = decision_log.await_row(lambda: {"id": "already-there"})
        self.assertEqual(result, {"id": "already-there"})
        self.assertLess(time.monotonic() - started, 0.05,
                        "a row that is already committed must return on the first fetch")

    def test_a_genuinely_missing_row_still_returns_none_within_the_budget(self):
        """A 404 must stay a 404 -- bounded, so an enumeration of bogus ids cannot pin
        threadpool workers open indefinitely."""
        os.environ["DECISION_READ_AFTER_WRITE_MS"] = "150"
        started = time.monotonic()
        self.assertIsNone(decision_log.await_row(lambda: None))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 2.0, "the wait is not bounded")

    def test_the_window_is_configurable_and_blank_aware(self):
        os.environ["DECISION_READ_AFTER_WRITE_MS"] = ""
        self.assertGreater(decision_log.read_after_write_seconds(), 0.0)
        os.environ["DECISION_READ_AFTER_WRITE_MS"] = "0"
        self.assertEqual(decision_log.read_after_write_seconds(), 0.0)

    def test_flush_no_longer_claims_to_make_reads_correct(self):
        """The docstring asserted a guarantee the code cannot provide across workers. Leaving
        it in place is how the next reader concludes there is nothing to fix here."""
        doc = (decision_log.flush.__doc__ or "").lower()
        self.assertIn("this process", doc, "flush() must scope its promise to this process")
        self.assertIn("worker", doc, "flush() must name the cross-worker case it does NOT cover")
        self.assertIn("await_row", doc, "flush() must point at what does cover it")

    def test_flush_still_waits_on_this_processes_own_writes(self):
        """The half that DID work must keep working."""
        landed = []

        def slow_write():
            time.sleep(0.1)
            landed.append(1)

        decision_log.submit(slow_write)
        decision_log.flush()
        self.assertEqual(landed, [1], "flush() stopped waiting on local pending writes")


if __name__ == "__main__":
    unittest.main()
