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

USAGE — one line, immediately after the assignment:

    app_main.storage = Storage(path=os.path.join(self.tempdir.name, "x.db"))
    restore_storage_after(self)

`addCleanup` rather than tearDown, deliberately. Cleanups run AFTER tearDown, so the module's own
teardown still runs against THIS test's storage — an earlier fix put the restore at the top of
tearDown and silently redirected every teardown statement at the restored global. It also works
for modules with no tearDown at all, or with several.
"""
from __future__ import annotations

import app.main as app_main

__all__ = ["restore_storage_after"]


def restore_storage_after(test_case) -> None:
    """Put back whatever `app_main.storage` was when this is called, once the test ends."""
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
