"""A retention window the UI advertises must either be enforced, or say loudly that it is not.

`archive_old_decisions` (scheduler.py) returned `{"skipped": "archiving_disabled"}` and nothing
else whenever `DECISION_ARCHIVE_SINK` was unset -- which is the default (`archiver.py:380`
treats "none" and "" as off). So on every default deployment:

  * the scheduled job ran, purged nothing, and logged nothing;
  * `settings.audit_retention_days` defaulted to 90 (`models.py:745`) and the settings screen
    rendered it as "Audit Retention Days" (`experience_studio.py:219`);
  * decisions were kept forever.

The disk consequence is measurable. On the soak stack: **13.2 KB per decision row** -- trace
6.1 KB, rule_results 2.5 KB, computed_variables 1.6 KB, scorecard_result 1.1 KB, payload_preview
0.6 KB. 385k decisions occupied 4.9 GB, of which 4.2 GB was TOAST. At 1M decisions/day that is
~13 GB/day with no upper bound.

The compliance consequence is worse and is the reason this is a test rather than a note: a
workspace that set a retention window believes data older than it is gone. It is not. For a
service holding credit-bureau data that is a false statement about data handling, made by the
product's own settings screen.

Archiving (copy to OLAP) and retention (honour the window) are different jobs, and flipping the
default to delete would destroy data on upgrade -- so the no-op stays and is now loud. These
tests pin that it is loud, and that it goes quiet the moment either half is made consistent.
"""
from __future__ import annotations

import logging
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


class RetentionHonestyTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "ret.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, sink: str = "none"):
        """Capture with an explicit handler rather than assertLogs.

        assertLogs fails outright when no record arrives, which makes "did it warn?" and
        "is logging globally suppressed right now?" the same failure -- and in a full-suite
        run something upstream suppresses it, so these tests passed alone and failed in the
        gate. The handler below also forces `disabled` and the level, so the assertion is
        about this code's behaviour and not about what 2,000 earlier tests did to the
        logging module.
        """
        logger = logging.getLogger("rulemind.scheduler")
        records: list = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        saved = (logger.disabled, logger.level, logger.propagate)
        logger.disabled = False
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": sink}):
                result = scheduler.archive_old_decisions(self.storage)
        finally:
            logger.removeHandler(handler)
            logger.disabled, logger.propagate = saved[0], saved[2]
            logger.setLevel(saved[1])
        return result, "\n".join(records)

    def test_it_warns_when_a_window_is_set_and_nothing_enforces_it(self):
        self.storage.update_settings({"audit_retention_days": 90}, tenant_id=self.tenant)
        result, logs = self._run(sink="none")
        self.assertEqual(result["skipped"], "archiving_disabled")
        self.assertGreaterEqual(result["retention_configured_tenants"], 1)
        self.assertIn("RETENTION IS ADVERTISED BUT NOT ENFORCED", logs)

    def test_the_count_names_how_many_workspaces_are_affected(self):
        """A single warning for N workspaces is the difference between 'someone's setting is
        decorative' and 'every customer on this deployment has been told something untrue'."""
        self.storage.update_settings({"audit_retention_days": 90}, tenant_id=self.tenant)
        other = self.storage.create_tenant("Retention Co")["id"]
        self.storage.update_settings({"audit_retention_days": 30}, tenant_id=other)
        result, _ = self._run(sink="none")
        self.assertGreaterEqual(result["retention_configured_tenants"], 2)

    def test_it_is_quiet_when_no_workspace_has_asked_for_retention(self):
        """Nothing was promised, so nothing is being broken. A warning here would be noise, and
        a warning nobody can act on is how real ones get filtered out."""
        self.storage.update_settings({"audit_retention_days": 0}, tenant_id=self.tenant)
        result, logs = self._run(sink="none")
        self.assertEqual(result.get("retention_configured_tenants"), 0)
        self.assertNotIn("RETENTION IS ADVERTISED BUT NOT ENFORCED", logs)


class DiscardSinkTest(unittest.TestCase):
    """`discard` is the option that did not exist: purge without an OLAP copy.

    `archiving_enabled()` gates the whole retention job on a sink, and the only non-test sinks
    are ClickHouse and S3 -- both lazy-importing a package that is NOT in requirements.txt
    (`archiver.py:151-163` documents that trap). So a deployment that simply wanted its
    retention window honoured, without standing up a data warehouse, had no way to say so.
    """

    def test_discard_enables_the_retention_job(self):
        from app import archiver

        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "discard"}):
            self.assertTrue(archiver.archiving_enabled())
            self.assertEqual(archiver.get_archiver().name, "discard")

    def test_none_still_means_off(self):
        """The default must not change. Making "none" purge would delete data on upgrade with
        nobody having asked for it."""
        from app import archiver

        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "none"}):
            self.assertFalse(archiver.archiving_enabled())

    def test_it_needs_no_optional_dependency(self):
        """The other half of why it is useful: every other real sink needs a package the shipped
        image does not have, so `available()` is where they fail."""
        from app import archiver

        ok, reason = archiver.DiscardArchiver().available()
        self.assertTrue(ok, reason)

    def test_choosing_it_warns_that_data_is_destroyed(self):
        """Captured with an explicit handler, not assertLogs.

        This used assertLogs and passed alone while failing in the full gate with
        "no logs of level WARNING or higher triggered on rulemind.archiver" -- the identical
        fragility already fixed in RetentionHonestyTest._run, in this same file, which I did
        not carry across. assertLogs makes "did it warn?" and "is logging globally suppressed
        right now?" the same failure, and something in a 2,149-test run suppresses it.
        """
        from app import archiver

        logger = logging.getLogger("rulemind.archiver")
        records: list = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        saved = (logger.disabled, logger.level)
        logger.disabled = False
        logger.setLevel(logging.WARNING)
        logger.addHandler(handler)
        try:
            archiver.DiscardArchiver().available()
        finally:
            logger.removeHandler(handler)
            logger.disabled = saved[0]
            logger.setLevel(saved[1])
        self.assertIn("DELETED with no copy kept", "\n".join(records))

    def test_it_writes_nothing(self):
        from app import archiver

        self.assertIsNone(archiver.DiscardArchiver().write([{"id": "d1"}]))


if __name__ == "__main__":
    unittest.main()
