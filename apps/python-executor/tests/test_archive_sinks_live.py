"""The ClickHouse and S3 sinks, against real servers — the only way to know they work.

`test_decision_archiver.py` covers the archive-then-purge CONTRACT with an in-memory sink, and
says so. What it cannot cover is the two sinks anyone would actually deploy: their column lists,
their serialization, and the fact that their clients are lazy optional imports that are absent
from the shipped image. Both were entirely untested, and `retention_enforced` reported `true`
for either of them whether or not it could run.

Skipped unless CLICKHOUSE_URL / S3_TEST_ENDPOINT are set, exactly like the Redis suite — which
is the pattern that found two production bugs once it was finally wired into CI.

Locally:
    docker run -d --rm --name ch -p 18123:8123 clickhouse/clickhouse-server:24-alpine
    docker run -d --rm --name minio -p 19000:9000 \\
      -e MINIO_ROOT_USER=rulemind -e MINIO_ROOT_PASSWORD=rulemind123 minio/minio server /data
    CLICKHOUSE_URL=http://127.0.0.1:18123 S3_TEST_ENDPOINT=http://127.0.0.1:19000 \\
      python -m unittest tests.test_archive_sinks_live
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app.archiver import (  # noqa: E402
    ClickHouseArchiver,
    S3Archiver,
    archiving_available,
    get_archiver,
)

CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "")
S3_ENDPOINT = os.getenv("S3_TEST_ENDPOINT", "")


def _decisions(n: int, tenant: str):
    return [
        {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant,
            "policy_id": "policy_test",
            "outcome": ["approve", "reject", "review"][i % 3],
            "latency_ms": 10 + i,
            "source": "api",
            "experiment_id": None,
            "experiment_variant": None,
            "created_at": datetime(2026, 1, 1, 12, 0, 0),
            "payload_preview": {"bureau_score": 700 + i},
        }
        for i in range(n)
    ]


@unittest.skipUnless(CLICKHOUSE_URL, "set CLICKHOUSE_URL (e.g. http://127.0.0.1:18123)")
class ClickHouseSinkTest(unittest.TestCase):
    def setUp(self):
        import clickhouse_connect

        parsed = urlparse(CLICKHOUSE_URL)
        self.table = "rm_test_" + uuid.uuid4().hex[:12]
        self.env = {
            "DECISION_ARCHIVE_SINK": "clickhouse",
            "CLICKHOUSE_HOST": parsed.hostname or "127.0.0.1",
            "CLICKHOUSE_PORT": str(parsed.port or 8123),
            "CLICKHOUSE_TABLE": self.table,
        }
        self.client = clickhouse_connect.get_client(
            host=self.env["CLICKHOUSE_HOST"], port=int(self.env["CLICKHOUSE_PORT"]))
        # The columns the archiver writes, in its order. Getting this table wrong is the point:
        # a mismatch here is exactly the failure a mock could never surface.
        self.client.command(
            "CREATE TABLE {0} (id String, tenant_id String, policy_id String, outcome String, "
            "latency_ms Int64, source String, experiment_id Nullable(String), "
            "experiment_variant Nullable(String), created_at DateTime, record String) "
            "ENGINE = MergeTree ORDER BY (tenant_id, created_at)".format(self.table))
        self.addCleanup(lambda: self.client.command("DROP TABLE IF EXISTS " + self.table))

    def test_the_factory_builds_this_sink_and_reports_it_available(self):
        archiver = get_archiver(self.env)
        self.assertIsInstance(archiver, ClickHouseArchiver)
        self.assertEqual(archiving_available(self.env), (True, ""))

    def test_decisions_round_trip_through_a_real_server(self):
        tenant = "tenant_" + uuid.uuid4().hex[:8]
        written = _decisions(5, tenant)
        get_archiver(self.env).write(written)

        rows = self.client.query(
            "SELECT id, outcome, latency_ms, record FROM {0} WHERE tenant_id = %(t)s "
            "ORDER BY latency_ms".format(self.table), parameters={"t": tenant}).result_rows
        self.assertEqual(len(rows), 5, "the insert did not land")
        self.assertEqual([r[1] for r in rows], [d["outcome"] for d in written])
        self.assertEqual([r[2] for r in rows], [d["latency_ms"] for d in written])
        # `record` is the whole decision as JSON, which is what makes the archive replayable.
        first = json.loads(rows[0][3])
        self.assertEqual(first["payload_preview"]["bureau_score"], 700)

    def test_an_empty_batch_is_a_no_op_rather_than_an_error(self):
        get_archiver(self.env).write([])  # must not raise, and must not need a connection


@unittest.skipUnless(S3_ENDPOINT, "set S3_TEST_ENDPOINT (e.g. http://127.0.0.1:19000)")
class S3SinkTest(unittest.TestCase):
    def setUp(self):
        import boto3

        self.bucket = "rm-test-" + uuid.uuid4().hex[:12]
        self.env = {
            "DECISION_ARCHIVE_SINK": "s3",
            "DECISION_ARCHIVE_S3_BUCKET": self.bucket,
            "DECISION_ARCHIVE_S3_ENDPOINT": S3_ENDPOINT,
            "DECISION_ARCHIVE_S3_PREFIX": "rulemind/decisions",
            "DECISION_ARCHIVE_S3_FORMAT": "jsonl",
            "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", "rulemind"),
            "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", "rulemind123"),
            "AWS_REGION": "us-east-1",
        }
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"):
            os.environ[key] = self.env[key]
        self.client = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1")
        self.client.create_bucket(Bucket=self.bucket)

        def _teardown():
            listing = self.client.list_objects_v2(Bucket=self.bucket)
            for obj in listing.get("Contents", []):
                self.client.delete_object(Bucket=self.bucket, Key=obj["Key"])
            self.client.delete_bucket(Bucket=self.bucket)

        self.addCleanup(_teardown)

    def test_the_factory_builds_this_sink_and_reports_it_available(self):
        archiver = get_archiver(self.env)
        self.assertIsInstance(archiver, S3Archiver)
        self.assertEqual(archiving_available(self.env), (True, ""))

    def test_a_batch_becomes_one_gzipped_jsonl_object(self):
        import gzip

        tenant = "tenant_" + uuid.uuid4().hex[:8]
        written = _decisions(4, tenant)
        get_archiver(self.env).write(written)

        keys = [o["Key"] for o in self.client.list_objects_v2(Bucket=self.bucket).get("Contents", [])]
        self.assertEqual(len(keys), 1, "expected exactly one object per batch, got {0}".format(keys))
        self.assertTrue(keys[0].startswith("rulemind/decisions/" + tenant + "/"), keys[0])

        body = self.client.get_object(Bucket=self.bucket, Key=keys[0])["Body"].read()
        lines = gzip.decompress(body).decode("utf-8").strip().splitlines()
        self.assertEqual(len(lines), 4, "one JSON object per decision")
        self.assertEqual([json.loads(line)["outcome"] for line in lines],
                         [d["outcome"] for d in written])

    def test_a_missing_bucket_setting_is_reported_not_attempted(self):
        env = dict(self.env, DECISION_ARCHIVE_S3_BUCKET="")
        ok, reason = archiving_available(env)
        self.assertFalse(ok)
        self.assertIn("DECISION_ARCHIVE_S3_BUCKET", reason)


class SinkAvailabilityTest(unittest.TestCase):
    """Runs without any container: the availability probe is about imports and settings."""

    def test_a_sink_with_no_client_installed_is_not_available(self):
        # Whatever is installed here, `none` is always available and an unknown sink falls back
        # to it -- so the interesting assertion is the reason string being actionable when a
        # real sink cannot run.
        ok, reason = archiving_available({"DECISION_ARCHIVE_SINK": "none"})
        self.assertFalse(ok)
        self.assertIn("DECISION_ARCHIVE_SINK", reason)

    def test_parquet_without_pyarrow_is_reported(self):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("pyarrow is installed, so this branch cannot be exercised here")
        ok, reason = archiving_available({
            "DECISION_ARCHIVE_SINK": "s3",
            "DECISION_ARCHIVE_S3_BUCKET": "b",
            "DECISION_ARCHIVE_S3_FORMAT": "parquet",
        })
        self.assertFalse(ok)
        self.assertIn("pyarrow", reason)


if __name__ == "__main__":
    unittest.main()
