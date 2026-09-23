"""Retention is a promise measured in CALENDAR months, and days cannot encode it (P16).

Retention was days-only: `settings.audit_retention_days`, default 90, set over HTTP as
`retention_days`. But nobody buys 2555 days of retention. Contracts and regulators say "retain
decisions for 7 years" and "purge PII after 24 months", and the operator translating that into
the box on the settings screen multiplies by 365.

That translation is wrong, silently, in the deleting direction. Seven years measured against the
real calendar is 2557 days — the 2555 an operator types is short by the two leap days in the
span. The purge job then deletes two days of decisions that the workspace was contractually
required to still have. Nothing errors: the sink accepts them, the rows go away, the settings
screen still reads "2555", and the gap only surfaces when a regulator asks for a decision from
the wrong end of the window and the answer is that it does not exist.

The fix is to store months and do calendar arithmetic. Three things then have to hold, and each
is a test below:

* **A month is not 30 days.** `add_months` walks the actual calendar, so 24 months back from any
  date spans 730 or 731 days, never 720. The end-of-month case (31 March minus a month) is
  CLAMPED to the last valid day of the target month — 28 February, not 3 March — matching
  dateutil/PostgreSQL/java.time, and erring toward retaining longer rather than purging early.
* **Existing tenants keep the window they have.** `audit_retention_months` is NULL for every row
  that predates it and there is no backfill, so `audit_retention_days` stays authoritative and
  the cutoff for those tenants is bit-for-bit what it was before. A months-aware system that
  quietly ignored the days column would purge nothing for every existing workspace.
* **The migration is idempotent.** `Storage.__init__` runs `create_all`, so on a fresh database
  the column already exists when the chain runs; an unguarded `add_column` aborts the upgrade.

The last group covers the cold-storage sinks, which had no test at all: the suite exercised only
`MemoryArchiver` and said so in its own docstring, so `ClickHouseArchiver` and `S3Archiver` — the
two sinks anyone actually deploys — could have been shaped wrong (a column list out of step with
the row tuples inserts garbage or raises on the first real batch) and CI would have been green.
Their clients are lazy-imported, so a stubbed module pins the insert contract without needing a
ClickHouse or an S3.
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, update  # noqa: E402

import app.main as app_main  # noqa: E402
from app.archiver import (  # noqa: E402
    MAX_RETENTION_MONTHS,
    ClickHouseArchiver,
    S3Archiver,
    add_months,
    normalize_retention_months,
    retention_cutoff,
    retention_window_days,
)
from app.models import Base, Decision  # noqa: E402
from app.scheduler import archive_old_decisions  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(APP_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


class CalendarMonthArithmeticTests(unittest.TestCase):
    """A month is a calendar month. Every assertion here fails if it is treated as 30 days."""

    def test_a_month_is_not_thirty_days(self):
        # 24 months back from a fixed date lands on the same day-of-month two years earlier.
        # 24 * 30 = 720 days would land on 2026-01-14 — over two weeks of decisions purged early.
        now = datetime(2026, 1, 4, 12, 0, 0)
        self.assertEqual(add_months(now, -24), datetime(2024, 1, 4, 12, 0, 0))
        self.assertNotEqual(add_months(now, -24), now - timedelta(days=720))
        self.assertEqual((now - add_months(now, -24)).days, 731, "spans a leap day")

    def test_seven_years_is_2557_days_not_2555(self):
        # The exact hand-conversion error this feature exists to remove: 7 * 365 = 2555 misses
        # the two leap days (2020-02-29 and 2024-02-29) inside the span.
        now = datetime(2026, 9, 4)
        self.assertEqual(add_months(now, -84), datetime(2019, 9, 4))
        self.assertEqual(retention_window_days(84, now=now), 2557)
        self.assertNotEqual(retention_window_days(84, now=now), 7 * 365)

    def test_end_of_month_clamps_to_the_last_valid_day(self):
        # The deliberate choice: clamp to the target month's last day rather than overflow into
        # the next month. Matches relativedelta / PostgreSQL / java.time / .NET.
        self.assertEqual(add_months(datetime(2025, 1, 31), 1), datetime(2025, 2, 28))
        self.assertEqual(add_months(datetime(2024, 1, 31), 1), datetime(2024, 2, 29), "leap year")
        self.assertEqual(add_months(datetime(2025, 3, 31), -1), datetime(2025, 2, 28))
        self.assertEqual(add_months(datetime(2025, 5, 31), -1), datetime(2025, 4, 30))
        # 24 months from 31 January lands on a real 31 January, not a rolled-over 2/3 March.
        self.assertEqual(add_months(datetime(2024, 1, 31), 24), datetime(2026, 1, 31))

    def test_clamping_errs_toward_retaining_longer(self):
        # Subtracting a month from 31 March gives 28 Feb (clamped), not 3 Mar (overflowed).
        # The clamped cutoff is EARLIER, so it purges less — never less retention than promised.
        clamped = add_months(datetime(2025, 3, 31), -1)
        overflowed = datetime(2025, 2, 28) + timedelta(days=3)
        self.assertLess(clamped, overflowed)

    def test_year_and_leap_boundaries(self):
        self.assertEqual(add_months(datetime(2025, 1, 15), -1), datetime(2024, 12, 15))
        self.assertEqual(add_months(datetime(2024, 12, 15), 1), datetime(2025, 1, 15))
        self.assertEqual(add_months(datetime(2024, 2, 29), -12), datetime(2023, 2, 28))
        self.assertEqual(add_months(datetime(2024, 2, 29), 48), datetime(2028, 2, 29))
        # Time-of-day survives the shift; the cutoff is an instant, not a date.
        self.assertEqual(add_months(datetime(2025, 6, 10, 3, 45, 9), -3), datetime(2025, 3, 10, 3, 45, 9))


class RetentionCutoffTests(unittest.TestCase):
    """Which unit applies, and what happens when neither does."""

    NOW = datetime(2026, 9, 4, 8, 0, 0)

    def test_months_win_over_days_when_set(self):
        cutoff = retention_cutoff({"audit_retention_months": 24, "audit_retention_days": 90}, now=self.NOW)
        self.assertEqual(cutoff, datetime(2024, 9, 4, 8, 0, 0))

    def test_legacy_days_still_govern_when_months_are_unset(self):
        # The compatibility guarantee: a tenant that predates months keeps the exact window it
        # had. Ignoring audit_retention_days would silently stop purging for every such tenant.
        for months in (None, 0, -3, "", "not-a-number"):
            with self.subTest(months=months):
                cutoff = retention_cutoff({"audit_retention_months": months, "audit_retention_days": 90}, now=self.NOW)
                self.assertEqual(cutoff, self.NOW - timedelta(days=90))

    def test_settings_without_the_months_key_at_all_behave_as_before(self):
        self.assertEqual(retention_cutoff({"audit_retention_days": 45}, now=self.NOW), self.NOW - timedelta(days=45))

    def test_retention_disabled_returns_none_so_nothing_is_purged(self):
        # None means "purge nothing". A far-past datetime here would delete the whole log.
        for settings in ({}, {"audit_retention_days": 0}, {"audit_retention_days": None},
                         {"audit_retention_days": "junk"}, {"audit_retention_days": -1}):
            with self.subTest(settings=settings):
                self.assertIsNone(retention_cutoff(settings, now=self.NOW))

    def test_absurd_month_counts_are_clamped_not_crashed(self):
        # add_months rewrites the year field; an unclamped 10_000_000 would raise ValueError
        # inside the purge loop rather than being bounded.
        self.assertEqual(normalize_retention_months(10_000_000), MAX_RETENTION_MONTHS)
        self.assertEqual(retention_cutoff({"audit_retention_months": 10_000_000}, now=self.NOW),
                         add_months(self.NOW, -MAX_RETENTION_MONTHS))

    def test_retention_window_days_is_zero_for_unused_months(self):
        self.assertEqual(retention_window_days(None), 0)
        self.assertEqual(retention_window_days(0), 0)


class RetentionSettingsPersistenceTests(unittest.TestCase):
    """Storage is the only place that resolves months-vs-days on write, so the days-only
    endpoints and the dashboard keep working untouched."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "retention.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def test_existing_tenants_start_with_no_month_setting(self):
        settings = self.storage.get_settings(tenant_id=self.tenant)
        self.assertIsNone(settings["audit_retention_months"], "NULL until somebody sets it")
        self.assertEqual(settings["audit_retention_days"], 90, "the pre-existing window is untouched")

    def test_setting_months_mirrors_a_truthful_day_count(self):
        saved = self.storage.update_settings({"audit_retention_months": 84}, tenant_id=self.tenant)
        self.assertEqual(saved["audit_retention_months"], 84)
        # The mirror is measured, not multiplied, so days-speaking readers see 2557-ish, not 2555.
        self.assertEqual(saved["audit_retention_days"], retention_window_days(84))
        self.assertGreater(saved["audit_retention_days"], 7 * 365)

    def test_setting_days_alone_reverts_the_workspace_to_days(self):
        self.storage.update_settings({"audit_retention_months": 24}, tenant_id=self.tenant)
        saved = self.storage.update_settings({"audit_retention_days": 45}, tenant_id=self.tenant)
        self.assertIsNone(saved["audit_retention_months"], "an explicit days write picks days")
        self.assertEqual(saved["audit_retention_days"], 45)

    def test_zero_months_clears_back_to_days(self):
        self.storage.update_settings({"audit_retention_months": 24}, tenant_id=self.tenant)
        saved = self.storage.update_settings({"audit_retention_months": 0, "audit_retention_days": 30},
                                             tenant_id=self.tenant)
        self.assertIsNone(saved["audit_retention_months"])
        self.assertEqual(saved["audit_retention_days"], 30)

    def test_unrelated_patch_does_not_disturb_retention(self):
        self.storage.update_settings({"audit_retention_months": 24}, tenant_id=self.tenant)
        before = self.storage.get_settings(tenant_id=self.tenant)
        saved = self.storage.update_settings({"theme_mode": "dark"}, tenant_id=self.tenant)
        self.assertEqual(saved["audit_retention_months"], 24)
        self.assertEqual(saved["audit_retention_days"], before["audit_retention_days"])

    def test_config_export_import_round_trip_preserves_months(self):
        # export_config/replace_all is how a workspace is copied between environments; a window
        # that survived as days but lost its month unit would silently drift on the next edit.
        self.storage.update_settings({"audit_retention_months": 24}, tenant_id=self.tenant)
        exported = self.storage.export_config(tenant_id=self.tenant)
        self.storage.update_settings({"audit_retention_days": 7}, tenant_id=self.tenant)
        self.storage.replace_all(exported, tenant_id=self.tenant)
        restored = self.storage.get_settings(tenant_id=self.tenant)
        self.assertEqual(restored["audit_retention_months"], 24)
        self.assertEqual(restored["audit_retention_days"], retention_window_days(24))


class RetentionApiTests(unittest.TestCase):
    """The days-only HTTP surface (`/api/v1/settings/data-protection`) must stay coherent: it
    reports the real window for a months-configured workspace instead of a stale number."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "retention-api.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def test_data_protection_reports_the_month_window_in_days(self):
        app_main.storage.update_settings({"audit_retention_months": 24}, tenant_id=self.tenant)
        body = self.client.get("/api/v1/settings/data-protection", headers=self.headers).json()
        self.assertEqual(body["retention_days"], retention_window_days(24))
        self.assertGreaterEqual(body["retention_days"], 730, "24 calendar months, not 720 days")

    def test_settings_get_exposes_the_month_setting(self):
        app_main.storage.update_settings({"audit_retention_months": 12}, tenant_id=self.tenant)
        body = self.client.get("/api/v1/settings", headers=self.headers).json()
        self.assertEqual(body["audit_retention_months"], 12)

    def test_days_only_put_still_works_and_takes_over(self):
        app_main.storage.update_settings({"audit_retention_months": 24}, tenant_id=self.tenant)
        body = self.client.put("/api/v1/settings/data-protection", headers=self.headers,
                               json={"retention_days": 45}).json()
        self.assertEqual(body["retention_days"], 45)
        self.assertIsNone(app_main.storage.get_settings(tenant_id=self.tenant)["audit_retention_months"])


class RetentionMigrationTests(unittest.TestCase):
    """`Storage.__init__` and migration 0001 both run create_all, so the new column already
    exists when 0022 runs. An unguarded add_column aborts the whole chain — on a FRESH database,
    not only on an upgraded one."""

    def test_column_lands_on_a_fresh_upgrade_to_head(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = os.path.join(tmp, "fresh.db")
            url = "sqlite:///{0}".format(db)
            os.environ["DATABASE_URL"] = url
            try:
                command.upgrade(_alembic_config(url), "head")
            finally:
                os.environ.pop("DATABASE_URL", None)
            conn = sqlite3.connect(db)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
            conn.close()
            self.assertIn("audit_retention_months", cols)
            self.assertIn("audit_retention_days", cols, "the legacy column is kept, not replaced")

    def test_upgrade_is_idempotent_over_a_create_all_database(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = os.path.join(tmp, "existing.db")
            url = "sqlite:///{0}".format(db)
            Base.metadata.create_all(create_engine(url))
            os.environ["DATABASE_URL"] = url
            try:
                command.upgrade(_alembic_config(url), "head")  # must not raise "duplicate column"
            finally:
                os.environ.pop("DATABASE_URL", None)
            conn = sqlite3.connect(db)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
            conn.close()
            self.assertIn("audit_retention_months", cols)


class PurgeHonoursCalendarMonthsTests(unittest.TestCase):
    """The scheduler's archive/purge job is the enforcement point: this is where a 30-day month
    actually destroys data."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "purge.db"))
        self.tenant = self.storage.default_tenant_id
        self.policy = self.storage.list_policies(status="prod", tenant_id=self.tenant)[0]

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self, age_days: int) -> str:
        row = self.storage.add_decision(
            {"policy_id": self.policy["id"], "outcome": "approve", "source": "test",
             "payload_preview": {}},
            tenant_id=self.tenant,
        )
        with self.storage.connect() as session:
            session.execute(update(Decision).where(Decision.id == row["id"])
                            .values(created_at=datetime.utcnow() - timedelta(days=age_days)))
        return str(row["id"])

    def _alive(self, decision_id: str) -> bool:
        with self.storage.connect() as session:
            return session.get(Decision, decision_id) is not None

    def test_a_decision_in_the_thirty_day_gap_survives(self):
        # 24 calendar months is 730 or 731 days for ANY start date (two years contain at most one
        # leap day), while 24 * 30 is 720. A decision 725 days old therefore sits strictly inside
        # the real window and strictly outside the fake one: correct code keeps it, a 30-day
        # month deletes it. No dependence on today's date.
        inside_gap = self._seed(725)
        recent = self._seed(10)
        ancient = self._seed(900)
        self.storage.update_settings({"audit_retention_months": 24}, tenant_id=self.tenant)

        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "memory"}):
            result = archive_old_decisions(self.storage)

        self.assertEqual(result["sink"], "memory")
        self.assertFalse(self._alive(ancient), "900 days old is outside a 24-month window")
        self.assertTrue(self._alive(inside_gap),
                        "725 days is inside 24 calendar months (>=730d) — purging it breaks the promise")
        self.assertTrue(self._alive(recent))

    def test_legacy_days_tenant_purges_exactly_as_before(self):
        old = self._seed(100)
        fresh = self._seed(10)
        self.storage.update_settings({"audit_retention_days": 90}, tenant_id=self.tenant)
        self.assertIsNone(self.storage.get_settings(tenant_id=self.tenant)["audit_retention_months"])

        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "memory"}):
            archive_old_decisions(self.storage)

        self.assertFalse(self._alive(old))
        self.assertTrue(self._alive(fresh))

    def test_nothing_is_purged_when_retention_is_disabled(self):
        old = self._seed(5000)
        self.storage.update_settings({"audit_retention_days": 0}, tenant_id=self.tenant)
        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "memory"}):
            result = archive_old_decisions(self.storage)
        self.assertEqual(result["archived"], 0)
        self.assertTrue(self._alive(old))

    def test_no_sink_means_no_purge_at_all(self):
        # The default posture: with DECISION_ARCHIVE_SINK unset nothing is ever deleted, however
        # short the retention window is. Retention is only enforced where history has somewhere
        # to go.
        old = self._seed(5000)
        self.storage.update_settings({"audit_retention_months": 1}, tenant_id=self.tenant)
        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "none"}):
            result = archive_old_decisions(self.storage)
        # The KEY, not the whole dict. The job now also reports how many workspaces have a
        # retention window configured while nothing enforces it -- which is exactly the
        # situation this test constructs, and which used to be silent. Pinning the exact object
        # made adding that diagnostic a test failure in two separate files.
        self.assertEqual(result.get("skipped"), "archiving_disabled")
        self.assertEqual(result.get("retention_configured_tenants"), 1,
                         "this workspace HAS a window set and nothing is enforcing it -- the "
                         "job must say so rather than skipping quietly")
        self.assertTrue(self._alive(old))


class _StubClickHouseClient:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, column_names):
        self.inserts.append({"table": table, "rows": rows, "columns": column_names})


class _StubS3Client:
    def __init__(self):
        self.objects = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803 - boto3's own kwarg names
        self.objects.append({"bucket": Bucket, "key": Key, "body": Body})


class ColdStorageSinkTests(unittest.TestCase):
    """`ClickHouseArchiver` and `S3Archiver` had no coverage — the suite ran `MemoryArchiver` and
    said so. Their clients are lazy-imported, so stub modules pin the wire contract (which is
    where the bugs are: a column list out of step with the row tuples) without a real server."""

    RECORDS = [
        {"id": "d1", "tenant_id": "t1", "policy_id": "p1", "outcome": "approve", "latency_ms": None,
         "source": "api", "experiment_id": None, "experiment_variant": None,
         "created_at": "2026-09-04T10:11:12", "payload_preview": {"a": 1}},
        {"id": "d2", "tenant_id": "t1", "policy_id": "p1", "outcome": "reject", "latency_ms": 12,
         "source": "api", "experiment_id": "e1", "experiment_variant": "b",
         "created_at": "2026-09-04T10:11:13", "payload_preview": {}},
    ]

    def _clickhouse(self):
        client = _StubClickHouseClient()
        module = types.ModuleType("clickhouse_connect")
        module.captured = {}  # type: ignore[attr-defined]

        def get_client(**kwargs):
            module.captured = kwargs  # type: ignore[attr-defined]
            return client

        module.get_client = get_client  # type: ignore[attr-defined]
        return module, client

    def test_clickhouse_insert_columns_match_every_row(self):
        module, client = self._clickhouse()
        archiver = ClickHouseArchiver({"CLICKHOUSE_TABLE": "decisions_cold", "CLICKHOUSE_PORT": "9000",
                                       "CLICKHOUSE_HOST": "olap.internal", "CLICKHOUSE_DATABASE": "audit"})
        with mock.patch.dict(sys.modules, {"clickhouse_connect": module}):
            archiver.write(list(self.RECORDS))

        self.assertEqual(module.captured["port"], 9000, "port is an int, not the env string")
        self.assertEqual(module.captured["host"], "olap.internal")
        self.assertEqual(module.captured["database"], "audit")
        insert = client.inserts[0]
        self.assertEqual(insert["table"], "decisions_cold")
        self.assertEqual(len(insert["rows"]), 2)
        for row in insert["rows"]:
            self.assertEqual(len(row), len(insert["columns"]), "a short/long row corrupts the column mapping")
        first = dict(zip(insert["columns"], insert["rows"][0], strict=True))
        self.assertEqual(first["id"], "d1")
        self.assertEqual(first["latency_ms"], 0, "a missing latency must not insert NULL into a UInt column")
        # The whole record is preserved as JSON, so the archive is not lossier than the hot table.
        self.assertEqual(json.loads(first["record"])["payload_preview"], {"a": 1})

    def test_clickhouse_empty_batch_never_connects(self):
        archiver = ClickHouseArchiver({})
        with mock.patch.dict(sys.modules, {"clickhouse_connect": None}):
            archiver.write([])  # would raise on an attempted import if it connected

    def test_s3_writes_one_gzipped_ndjson_object_per_batch(self):
        client = _StubS3Client()
        module = types.ModuleType("boto3")
        module.client = lambda *args, **kwargs: client  # type: ignore[attr-defined]
        archiver = S3Archiver({"DECISION_ARCHIVE_S3_BUCKET": "cold-bucket",
                               "DECISION_ARCHIVE_S3_PREFIX": "/rulemind/decisions/"})
        with mock.patch.dict(sys.modules, {"boto3": module}):
            archiver.write(list(self.RECORDS))

        obj = client.objects[0]
        self.assertEqual(obj["bucket"], "cold-bucket")
        self.assertEqual(obj["key"], "rulemind/decisions/t1/20260904T101112-2.jsonl.gz",
                         "keys are tenant-partitioned and carry the batch timestamp + size")
        lines = gzip.decompress(obj["body"]).decode("utf-8").splitlines()
        self.assertEqual([json.loads(line)["id"] for line in lines], ["d1", "d2"])

    def test_s3_parquet_falls_back_to_ndjson_without_pyarrow(self):
        # pyarrow is optional. A deployment that asks for Parquet without it must still archive —
        # silently dropping the batch would purge rows that were never written anywhere.
        client = _StubS3Client()
        module = types.ModuleType("boto3")
        module.client = lambda *args, **kwargs: client  # type: ignore[attr-defined]
        archiver = S3Archiver({"DECISION_ARCHIVE_S3_BUCKET": "cold-bucket",
                               "DECISION_ARCHIVE_S3_FORMAT": "parquet"})
        with mock.patch.dict(sys.modules, {"boto3": module, "pyarrow": None}):
            archiver.write(list(self.RECORDS))
        self.assertTrue(client.objects[0]["key"].endswith(".jsonl.gz"))

    def test_s3_without_a_bucket_raises_instead_of_silently_dropping(self):
        # archive_and_purge_decisions purges only after write() returns; raising here is what
        # keeps a misconfigured sink from deleting the hot rows.
        with self.assertRaises(RuntimeError):
            S3Archiver({}).write(list(self.RECORDS))

    def test_s3_empty_batch_never_connects(self):
        with mock.patch.dict(sys.modules, {"boto3": None}):
            S3Archiver({}).write([])


if __name__ == "__main__":
    unittest.main()
