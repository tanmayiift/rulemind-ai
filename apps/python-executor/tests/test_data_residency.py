"""GET /api/v1/data-residency (plan 6.7): where data is, and every place it can go.

The report is assembled from live configuration, so each test configures a destination and checks
the report names it -- and the one thing it must never contain is a credential.
"""
from __future__ import annotations

import json
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import data_residency  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class DataResidencyTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "residency.db"))
        app_main.storage = self.storage
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.tenant = self.storage.default_tenant_id
        self.key = {"x-api-key": self.storage.generate_api_key_for_tenant(self.tenant)["plaintext"]}

    def _report(self):
        response = self.client.get("/api/v1/data-residency", headers=self.key)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _hosts(self, report, kind):
        return {item["host"] for item in report["egress"] if item["kind"] == kind}

    def test_the_declared_region_is_reported(self):
        with mock.patch.dict(os.environ, {"RULEMIND_DATA_REGION": "in-mumbai-1"}):
            self.assertEqual(self._report()["install_region"], "in-mumbai-1")

    def test_the_report_says_how_workspaces_are_kept_apart(self):
        """A report that only listed destinations would let a SQLite install -- or a Postgres one
        whose database user bypasses row security -- read as protected by the database."""
        isolation = self._report()["tenant_isolation"]
        if self.storage.engine.dialect.name == "postgresql":
            self.assertTrue(isolation["row_level_security"], isolation["detail"])
        else:
            self.assertFalse(isolation["row_level_security"])
            self.assertIn("SQLite", isolation["detail"])

    def test_connectors_and_their_token_endpoints_are_listed(self):
        self.storage.create_connector({"id": "bureau_x", "name": "B", "schema_paths": [], "sample_payload": {},
                                       "config": {"auth_type": "oauth2", "base_url": "https://api.bureau.example/v1",
                                                  "token_url": "https://auth.bureau.example/token",
                                                  "client_secret": "S3CRET", "fetch_at_decision": True}},
                                      tenant_id=self.tenant)
        report = self._report()
        self.assertIn("api.bureau.example", self._hosts(report, "connector"))
        self.assertIn("auth.bureau.example", self._hosts(report, "connector_token_endpoint"))
        self.assertNotIn("S3CRET", json.dumps(report))

    def test_webhook_subscriptions_are_listed(self):
        self.storage.create_webhook_subscription({"name": "partner", "url": "https://hooks.partner.example/rm",
                                                   "events": ["*"], "secret": "whsec"}, tenant_id=self.tenant)
        self.assertIn("hooks.partner.example", self._hosts(self._report(), "webhook_subscription"))

    def test_a_token_url_is_an_address_not_a_secret(self):
        """It matched the "token" marker and came back masked -- unreadable to its own author."""
        self.storage.create_connector({"id": "c_tok", "name": "T", "schema_paths": [], "sample_payload": {},
                                       "config": {"auth_type": "oauth2", "token_url": "https://auth.example/token",
                                                  "client_secret": "S3CRET"}}, tenant_id=self.tenant)
        config = self.client.get("/api/v1/connectors/c_tok", headers=self.key).json()["config"]
        self.assertEqual(config["token_url"], "https://auth.example/token")
        self.assertNotEqual(config["client_secret"], "S3CRET", "the real secret must still be masked")

    def test_the_archive_sink_and_broker_are_listed_when_configured(self):
        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "s3", "DECISION_ARCHIVE_S3_BUCKET": "rm-archive",
                                          "AWS_REGION": "ap-south-1", "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092"}):
            report = self._report()
        archive = [e for e in report["egress"] if e["kind"] == "decision_archive"][0]
        self.assertEqual((archive["host"], archive["region"]), ("rm-archive", "ap-south-1"))
        self.assertEqual(self._hosts(report, "kafka"), {"kafka-1,kafka-2"})

    def test_nothing_is_listed_that_is_not_configured(self):
        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "none"}, clear=False):
            os.environ.pop("KAFKA_BOOTSTRAP_SERVERS", None)
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            report = self._report()
        self.assertFalse([e for e in report["egress"] if e["kind"] in ("decision_archive", "kafka", "tracing")])

    def test_the_database_is_named_without_its_credentials(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://rulemind:hunter2@db.internal:5432/rulemind"}):
            database = data_residency._database()
        self.assertEqual((database["engine"], database["host"]), ("postgresql", "db.internal"))
        self.assertNotIn("hunter2", json.dumps(database))


if __name__ == "__main__":
    unittest.main()
