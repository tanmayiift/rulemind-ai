"""Restore `app.main.storage` after a test that replaces it.

WHY THIS EXISTS. Most test modules do `app_main.storage = Storage(path=<tempdir>)` in setUp so
the suite runs against a throwaway database. `app_main.storage` is a MODULE GLOBAL, so that
assignment outlives the test: when the tempdir is cleaned, the global is left pointing at a
database file that no longer exists, and **the next module that reads the global fails** with

    sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) unable to open database file

The failure lands on whoever reads the global next, never on the test that leaked it, and the
affected module always passes when run alone. That is why it looked like a random flake for
weeks and why chasing the failing test never found anything: **a test that only fails in company
is a shared resource running out — find what is shared, not what the failing test does.**

Two earlier attempts at this failed and must not be repeated. Capping `db._engine_cache` made it
WORSE (no cap ~1 error, FIFO 3, LRU 4, removed 8) and broke something else: FIFO evicts the
oldest engine, which is `app_main.storage`'s, so it disposed the shared engine out from under
live tests. Sweeping stale temp dirs helped (8 -> 1) but treats a symptom.

USAGE — one line, immediately BEFORE the assignment:

    preserve_storage_global(self)
    app_main.storage = Storage(path=os.path.join(self.tempdir.name, "x.db"))

**Before, not after.** It captures whatever the global is when it is called; calling it after the
assignment captures the test's own new Storage and restores that — a no-op that looks like a fix.
(Written the wrong way round once. The isolated runs still passed, because the module under test
never reads a global it did not set: only the NEXT module does.)

`addCleanup` rather than tearDown, deliberately. Cleanups run AFTER tearDown, so the module's own
teardown still runs against THIS test's storage — an earlier fix put the restore at the top of
tearDown and silently redirected every teardown statement at the restored global. It also works
for modules with no tearDown at all, or with several.
"""
from __future__ import annotations

import app.main as app_main

__all__ = ["preserve_storage_global"]


def preserve_storage_global(test_case) -> None:
    """Capture `app_main.storage` NOW, and put it back when this test ends.

    Call it BEFORE replacing the global -- see the module docstring.
    """
    previous = app_main.storage

    def _restore() -> None:
        # Release this test's connections before handing the global back. Without the dispose the
        # engine keeps file handles on a tempdir that is about to be removed.
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - cleanup must never fail a test
            pass
        try:
            app_main.storage = previous
        except Exception:  # pragma: no cover
            pass

    test_case.addCleanup(_restore)
