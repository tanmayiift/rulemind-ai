"""Reports builder — unit, smoke, regression.

* unit        — report generation (columns/filters/timezone/CSV) + the mailer
* smoke       — CRUD + preview + run + CSV export + column suggestions + send
* regression  — scheduled delivery records a last_run; email config round-trips
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app import mailer  # noqa: E402
from app.reports import generate_report, suggest_columns  # noqa: E402
from app.storage import Storage  # noqa: E402


def _decisions():
    now = datetime.now(timezone.utc)
    return [
        {"id": "d1", "policy_id": "p", "outcome": "approve", "latency_ms": 12, "source": "api",
         "created_at": now.isoformat(), "payload_preview": {"custom": {"amount": 5000}}, "computed_variables": {"score": 810}},
        {"id": "d2", "policy_id": "p", "outcome": "reject", "latency_ms": 9, "source": "api",
         "created_at": now.isoformat(), "payload_preview": {"custom": {"amount": 200}}, "computed_variables": {"score": 480}},
        {"id": "d3", "policy_id": "p", "outcome": "approve", "latency_ms": 15, "source": "sdk",
         "created_at": (now - timedelta(days=40)).isoformat(), "payload_preview": {}, "computed_variables": {"score": 700}},
    ]


class ReportUnitTests(unittest.TestCase):
    def test_dynamic_columns_project_inputs_and_outputs(self):
        defn = {"columns": [
            {"key": "outcome", "path": "outcome"},
            {"key": "score", "path": "computed_variables.score"},
            {"key": "amount", "path": "payload_preview.custom.amount"},
        ], "filters": {}, "timezone": "UTC"}
        out = generate_report(defn, _decisions())
        self.assertEqual(out["rows"][0], {"outcome": "approve", "score": 810, "amount": 5000})

    def test_outcome_filter(self):
        defn = {"columns": [{"key": "outcome", "path": "outcome"}], "filters": {"outcomes": ["reject"]}}
        out = generate_report(defn, _decisions())
        self.assertEqual(out["row_count"], 1)
        self.assertEqual(out["rows"][0]["outcome"], "reject")

    def test_time_window_filter(self):
        defn = {"columns": [{"key": "id", "path": "id"}], "filters": {"days": 7}}
        out = generate_report(defn, _decisions())
        ids = {r["id"] for r in out["rows"]}
        self.assertIn("d1", ids)
        self.assertNotIn("d3", ids)  # 40 days old, outside the 7-day window

    def test_timezone_formatting_and_csv(self):
        defn = {"columns": [{"key": "created_at", "label": "Time", "path": "created_at"}], "timezone": "Asia/Kolkata"}
        out = generate_report(defn, _decisions())
        self.assertTrue(out["csv"].startswith("Time"))
        self.assertIn("IST", out["csv"] + str(out["rows"]))  # timezone applied

    def test_suggest_columns_includes_dynamic_paths(self):
        cols = suggest_columns(_decisions())
        paths = {c["path"] for c in cols}
        self.assertIn("computed_variables.score", paths)
        self.assertIn("outcome", paths)


class MailerUnitTests(unittest.TestCase):
    def setUp(self):
        mailer.OUTBOX.clear()
        self._orig = mailer._SMTP_FACTORY

    def tearDown(self):
        mailer._SMTP_FACTORY = self._orig
        mailer.OUTBOX.clear()

    def test_outbox_when_not_configured(self):
        res = mailer.send_report_email(None, ["a@b.com"], "S", "B", "col\n1\n")
        self.assertFalse(res["delivered"])
        self.assertEqual(res["transport"], "outbox")
        self.assertEqual(len(mailer.OUTBOX), 1)

    def test_smtp_send_with_injected_transport(self):
        sent = {}

        class FakeSMTP:
            def send_message(self, msg): sent["to"] = msg["To"]
            def quit(self): sent["quit"] = True

        mailer._SMTP_FACTORY = lambda cfg: FakeSMTP()
        res = mailer.send_report_email(
            {"host": "smtp.example.com", "from_addr": "r@x.com"}, ["a@b.com"], "S", "B", "col\n1\n")
        self.assertTrue(res["delivered"])
        self.assertEqual(res["transport"], "smtp")
        self.assertEqual(sent["to"], "a@b.com")

    def test_no_recipients_is_noop(self):
        res = mailer.send_report_email(None, [], "S", "B", "x")
        self.assertFalse(res["delivered"])
        self.assertEqual(res["transport"], "none")


class ReportApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "rep.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        mailer.OUTBOX.clear()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _seed_decisions(self, n=3):
        for _ in range(n):
            self.client.post("/api/v1/decide", headers=self.headers,
                             json={"policyId": "policy_instant_personal_loan", "payload": {"bureau_score": 780}})

    def _create(self, **over):
        body = {"name": "Daily decisions", "columns": [
            {"key": "id", "label": "ID", "path": "id"},
            {"key": "outcome", "label": "Outcome", "path": "outcome"},
        ], "filters": {"days": 30}, "timezone": "UTC"}
        body.update(over)
        r = self.client.post("/api/v1/reports", headers=self.headers, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_crud_and_run(self):
        self._seed_decisions()
        rep = self._create()
        rid = rep["id"]
        run = self.client.post(f"/api/v1/reports/{rid}/run", headers=self.headers).json()
        self.assertGreaterEqual(run["row_count"], 1)
        self.assertIn("outcome", run["rows"][0])
        listing = self.client.get("/api/v1/reports", headers=self.headers).json()
        self.assertTrue(any(r["id"] == rid for r in listing))
        self.assertEqual(self.client.delete(f"/api/v1/reports/{rid}", headers=self.headers).status_code, 200)

    def test_csv_export(self):
        self._seed_decisions()
        rid = self._create()["id"]
        resp = self.client.get(f"/api/v1/reports/{rid}/export.csv", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["content-type"])
        self.assertTrue(resp.text.startswith("ID,Outcome"))

    def test_preview_and_column_suggestions(self):
        self._seed_decisions()
        prev = self.client.post("/api/v1/reports/preview", headers=self.headers,
                                json={"columns": [{"key": "outcome", "path": "outcome"}], "filters": {}, "timezone": "UTC"}).json()
        self.assertIn("rows", prev)
        sug = self.client.get("/api/v1/reports/column-suggestions", headers=self.headers).json()
        self.assertTrue(any(c["path"] == "outcome" for c in sug["columns"]))

    def test_send_uses_outbox_without_smtp(self):
        self._seed_decisions()
        rid = self._create(schedule={"enabled": True, "cron": "0 9 * * *", "recipients": ["ops@acme.com"]})["id"]
        res = self.client.post(f"/api/v1/reports/{rid}/send", headers=self.headers).json()
        self.assertEqual(res["delivery"]["transport"], "outbox")  # no SMTP configured
        self.assertEqual(mailer.OUTBOX[-1]["to"], ["ops@acme.com"])
        # last_run recorded
        self.assertIsNotNone(self.client.get(f"/api/v1/reports/{rid}", headers=self.headers).json()["last_run"])

    def test_email_config_roundtrip_masks_password(self):
        self.client.put("/api/v1/reports/email-config", headers=self.headers,
                        json={"host": "smtp.acme.com", "from_addr": "r@acme.com", "username": "u", "password": "SECRET"})
        cfg = self.client.get("/api/v1/reports/email-config", headers=self.headers).json()
        self.assertTrue(cfg["configured"])
        self.assertTrue(cfg["password_set"])
        self.assertNotIn("SECRET", str(cfg))

    def test_scheduled_delivery_records_last_run(self):
        import asyncio
        from app.scheduler import deliver_scheduled_report

        self._seed_decisions()
        rid = self._create(schedule={"enabled": True, "cron": "0 9 * * *", "recipients": ["ops@acme.com"]})["id"]
        tenant = app_main.storage.default_tenant_id
        delivery = asyncio.run(deliver_scheduled_report(app_main.storage, rid, tenant))
        self.assertIn(delivery["transport"], {"outbox", "smtp"})
        self.assertIsNotNone(app_main.storage.get_report(rid, tenant_id=tenant)["last_run"])


if __name__ == "__main__":
    unittest.main()
