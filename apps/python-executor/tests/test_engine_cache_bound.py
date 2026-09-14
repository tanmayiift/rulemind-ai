"""The engine cache must be bounded, or a long test run exhausts SQLite file handles.

`db._engine_cache` is keyed by URL and, until this bound existed, nothing evicted. That is
CORRECT for a server -- a handful of URLs held for the process lifetime -- and a slow leak in a
test suite, where every `Storage(path=<fresh tempdir>)` mints a new key whose pool keeps SQLite
handles open against a directory the test then deletes.

HOW IT ACTUALLY SHOWED UP, which is why it went unfixed for so long: not as a failure in the
test that leaked, but as `sqlite3.OperationalError: unable to open database file` on a RANDOM
test in an unrelated module, roughly one per ~1900-test run. Three consecutive full runs failed
on three different tests (test_connector_fetch twice, test_connector_fetch_refuted once), and
each of those modules passes cleanly on its own. A test that only fails in company is a shared
resource running out, not a broken test.

`dispose_engine()` was the earlier answer and its docstring already described this exact
failure -- but it puts the obligation on every test module, and only 13 of 151 took it up.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import db  # noqa: E402


class EngineCacheIsBoundedTest(unittest.TestCase):
    def setUp(self):
        self._saved_engines = dict(db._engine_cache)
        self._saved_factories = dict(db._session_factory_cache)
        db._engine_cache.clear()
        db._session_factory_cache.clear()
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        for engine in list(db._engine_cache.values()):
            try:
                engine.dispose()
            except Exception:  # pragma: no cover
                pass
        db._engine_cache.clear()
        db._session_factory_cache.clear()
        # Restore whatever the rest of the suite had cached, so this test is invisible to it.
        db._engine_cache.update(self._saved_engines)
        db._session_factory_cache.update(self._saved_factories)
        self.tempdir.cleanup()

    def _make(self, n: int):
        for i in range(n):
            db.engine_for(os.path.join(self.tempdir.name, "db{0}.sqlite".format(i)),
                          force_sqlite=True)

    def test_the_cache_never_exceeds_its_cap(self):
        self._make(db._ENGINE_CACHE_MAX + 25)
        self.assertLessEqual(len(db._engine_cache), db._ENGINE_CACHE_MAX,
                             "the engine cache grew past its bound — the leak is back")

    def test_eviction_DISPOSES_rather_than_just_dropping_the_reference(self):
        """The distinction that matters. Dropping the dict entry frees the Python object but
        leaves the pool's SQLite file handles open until garbage collection gets to them, which
        is exactly the unbounded-handle problem in a slower costume."""
        first_path = os.path.join(self.tempdir.name, "first.sqlite")
        engine = db.engine_for(first_path, force_sqlite=True)
        # Force a real connection so the pool actually holds a handle to evict.
        engine.connect().close()

        self._make(db._ENGINE_CACHE_MAX + 5)

        self.assertNotIn(first_path, "".join(db._engine_cache.keys()),
                         "the oldest engine was not evicted")
        # A disposed pool reports no checked-in connections; an undisposed one still holds them.
        self.assertEqual(engine.pool.checkedin(), 0,
                         "the evicted engine's pool still holds connections — it was dropped, "
                         "not disposed")

    def test_the_session_factory_is_evicted_WITH_its_engine(self):
        """Evicting the engine alone would leave a live factory handing out connections from a
        pool nothing can reach — the same trap dispose_engine() documents.

        Asserts on the EVICTED KEY, not on cache sizes. A size comparison passes whether or not
        factories are evicted, because they are created lazily and there are simply fewer of
        them — a mutation that removed the `_session_factory_cache.pop` survived exactly that
        weaker assertion.
        """
        first_path = os.path.join(self.tempdir.name, "factory-first.sqlite")
        # session_factory() populates BOTH caches under the same key, so there is a factory to
        # orphan in the first place.
        db.session_factory(first_path, force_sqlite=True)
        first_key = next(k for k in db._session_factory_cache if "factory-first" in k)

        self._make(db._ENGINE_CACHE_MAX + 10)

        self.assertNotIn(first_key, db._engine_cache, "the oldest engine was not evicted")
        self.assertNotIn(first_key, db._session_factory_cache,
                         "the session factory outlived its engine — it now hands out "
                         "connections from a pool nothing else can reach or dispose")

    def test_a_server_sized_workload_evicts_nothing(self):
        """The cap must be inert in production. A real deployment uses one or two URLs for the
        process lifetime, and an engine being disposed underneath it would be a genuine bug —
        so this pins that the bound only engages at test-suite scale."""
        self._make(3)
        before = list(db._engine_cache.keys())
        for _ in range(20):
            self._make(3)  # same three paths, repeatedly
        self.assertEqual(list(db._engine_cache.keys()), before,
                         "a steady-state workload evicted an engine it was still using")


if __name__ == "__main__":
    unittest.main()
