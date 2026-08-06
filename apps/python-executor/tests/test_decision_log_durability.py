"""Durability + memory safety of the async decision-log path.

The async path must (a) never grow memory without bound when the DB is slower than intake,
(b) never silently drop a write under that backpressure, and (c) never lose a queued write on
a graceful shutdown. These tests exercise those guarantees against the real thread pool by
stubbing the write fn with controllable latency.
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


class DecisionLogDurabilityTests(unittest.TestCase):
    def setUp(self):
        os.environ["ASYNC_DECISION_LOG"] = "1"
        os.environ["DECISION_LOG_MAX_QUEUE"] = "4"
        os.environ["DECISION_LOG_WORKERS"] = "1"
        import app.decision_log as dl

        importlib.reload(dl)  # fresh pool/semaphore bound to this test's env
        self.dl = dl

    def tearDown(self):
        self.dl.shutdown()
        for key in ("DECISION_LOG_MAX_QUEUE", "DECISION_LOG_WORKERS", "ASYNC_DECISION_LOG"):
            os.environ.pop(key, None)

    def test_no_drop_and_bounded_under_backpressure(self):
        # 1 worker + a queue cap of 4. Worker-thread writes block on a gate to hold the queue full;
        # once the cap is reached, further submits run INLINE on the calling thread and complete
        # immediately (they must NOT block the submit loop). Nothing is dropped.
        done = []
        inline = []
        lock = threading.Lock()
        gate = threading.Event()

        def write(i):
            on_worker = threading.current_thread().name.startswith("decision-log")
            if on_worker:
                gate.wait(5.0)  # hold the single worker so the queue stays full
            else:
                with lock:
                    inline.append(i)
            with lock:
                done.append(i)

        total = 20
        for i in range(total):
            self.dl.submit(write, i)  # must return promptly for every i (inline ones included)

        # The cap is 4 (1 running on the worker + 3 queued); the remaining 16 must have run inline.
        with lock:
            self.assertGreaterEqual(len(inline), total - 4, "backpressure did not fall back to inline")

        gate.set()  # let the 4 queued/running worker writes finish
        self.dl.flush(timeout=5.0)

        with lock:
            self.assertEqual(sorted(done), list(range(total)), "a write was dropped under backpressure")

    def test_shutdown_flushes_pending(self):
        results = []
        lock = threading.Lock()

        def write(i):
            time.sleep(0.02)
            with lock:
                results.append(i)

        for i in range(10):
            self.dl.submit(write, i)
        # shutdown() must block until every queued write has landed.
        self.dl.shutdown(timeout=5.0)
        with lock:
            self.assertEqual(sorted(results), list(range(10)), "shutdown lost a queued write")

    def test_sync_mode_writes_inline(self):
        os.environ["ASYNC_DECISION_LOG"] = "0"
        importlib.reload(self.dl)
        ran = []
        self.dl.submit(lambda: ran.append(1))
        self.assertEqual(ran, [1], "sync mode must write inline immediately")


if __name__ == "__main__":
    unittest.main()
