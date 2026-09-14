"""The sandbox's worker processes must be reaped on shutdown.

Unlike `decision_log`'s THREAD pool, which dies with the interpreter, this pool spawns child
PROCESSES. A child outlives its parent unless something reaps it. ProcessPoolExecutor registers
its own atexit handler, but that runs only on a clean interpreter exit — not on SIGKILL, and not
when a dev server or a test run is interrupted.

WHAT THAT COST, measured on a developer machine 2026-09-14: **159 orphaned sandbox workers**
dating back three weeks, every one reparented to launchd. At 32KB each that is nearly free — but
one had been spinning at **99% CPU since 2026-08-29**, a full core burned continuously, and it
corrupted a performance baseline badly enough to read as an 83% regression.

This cannot happen inside a container: the PID namespace reaps every child when the container
dies, whatever the signal. It is a host-only leak, which is exactly why ~1900 tests and a CI that
runs everything in containers never saw it.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import sandbox  # noqa: E402

SIMPLE = (
    '@variable(source="custom")\n'
    "def v(payload, variables, apis):\n"
    "    return 1\n"
)


def _worker_pids(pool) -> list:
    return [p.pid for p in getattr(pool, "_processes", {}).values()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class ShutdownReapsWorkersTest(unittest.TestCase):
    def tearDown(self):
        # Never leave this test's own workers behind — that would be the bug under test.
        sandbox.shutdown_pool()

    def test_the_pool_actually_spawns_child_processes(self):
        """The control. Everything below asserts processes DIE, and an assertion that something
        is dead passes trivially if it was never alive."""
        sandbox.execute_variable(SIMPLE, {}, {})
        pids = _worker_pids(sandbox._get_pool())
        self.assertTrue(pids, "the sandbox ran without spawning a worker — the rest of this "
                              "file would pass vacuously")
        for pid in pids:
            self.assertTrue(_alive(pid), "worker %s was not running" % pid)

    def test_shutdown_pool_terminates_every_worker(self):
        sandbox.execute_variable(SIMPLE, {}, {})
        pids = _worker_pids(sandbox._get_pool())
        self.assertTrue(pids)

        sandbox.shutdown_pool()

        # terminate() is a signal, not a join; give the OS a moment to reap.
        deadline = time.time() + 10
        while time.time() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.1)

        still = [p for p in pids if _alive(p)]
        self.assertEqual(still, [], "workers %s survived shutdown — on a host they are now "
                                    "orphans, reparented to init" % still)

    def test_a_wedged_worker_is_ALREADY_dead_before_shutdown_runs(self):
        """Why `shutdown_pool`'s terminate loop is defensive rather than load-bearing.

        This test started life asserting the opposite — that shutdown kills a worker spinning on
        untrusted code — because removing the terminate loop passes every other test here and I
        wanted the mutation to die. It does not, and the reason is worth pinning rather than
        papering over:

          1. An idle worker is reaped when the executor is finalised, which CPython's refcounting
             does the instant `_pool = None` drops the last reference. So for a healthy pool,
             "terminate then drop" and "just drop" are genuinely indistinguishable.
          2. A WEDGED worker never reaches shutdown, because `execute_variable`'s timeout already
             calls `_recycle_pool`, which terminates it. By the time anything shuts down, the
             pool has been replaced by a fresh one with no workers spawned yet — which is what
             this test asserts.

        So the terminate loop is belt-and-braces for a case the timeout path already covers, and
        the honest reading of the surviving mutation is "this line is defensive", not "this test
        is weak". It stays because it makes shutdown deterministic instead of dependent on GC
        timing, which matters on a fast SIGTERM.

        What was NEVER covered, and still is not coverable from inside the process, is SIGKILL:
        no handler runs, and the children are orphaned. That is the 159 orphans on this machine,
        and the only real fix is a PID namespace — i.e. run it in a container, as production does.
        """
        wedged = (
            '@variable(source="custom")\n'
            "def v(payload, variables, apis):\n"
            "    while True:\n"
            "        pass\n"
        )
        sandbox.execute_variable(wedged, {}, {}, timeout_ms=400)

        # _recycle_pool already ran: the wedged pool is gone and its replacement is empty until
        # the next submit. If this ever starts finding workers, the timeout path stopped
        # recycling and shutdown_pool's terminate loop becomes load-bearing again.
        self.assertEqual(_worker_pids(sandbox._get_pool()), [],
                         "a wedged worker survived the timeout recycle")

    def test_shutdown_drops_the_cached_pool_so_the_next_call_rebuilds(self):
        """Terminating the workers while keeping the handle would leave a permanently broken
        pool: every later submit raises BrokenProcessPool and the sandbox is dead until a
        restart. That is the bug `_get_pool` was already fixed for once."""
        sandbox.execute_variable(SIMPLE, {}, {})
        sandbox.shutdown_pool()
        self.assertIsNone(sandbox._pool, "the terminated pool is still cached")

        # And it must genuinely work again, not merely be non-None.
        self.assertEqual(sandbox.execute_variable(SIMPLE, {}, {}).get("value"), 1)

    def test_shutdown_is_idempotent_and_safe_with_no_pool(self):
        """It runs on the FastAPI shutdown event, which fires whether or not the sandbox was
        ever used — and a shutdown path that raises is worse than one that leaks."""
        sandbox.shutdown_pool()
        sandbox.shutdown_pool()   # no pool at all
        sandbox.execute_variable(SIMPLE, {}, {})
        sandbox.shutdown_pool()
        sandbox.shutdown_pool()   # twice after a real pool


if __name__ == "__main__":
    unittest.main()
