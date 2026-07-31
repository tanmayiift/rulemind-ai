"""JSONL simulation import/export — POST /api/v1/decide/batch/jsonl.

Large what-if runs are driven by uploading a `.jsonl` of payloads (one JSON object per line)
and optionally downloading the results as JSONL. A blank line is skipped; an unparseable line
becomes an `error` row rather than sinking the whole batch. Results match the JSON batch path.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402


class SimulationJsonlTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        prod = app_main.storage.list_policies(status="prod", tenant_id=app_main.storage.default_tenant_id)
        self.policy = prod[0]

    def _jsonl(self, *payloads: dict) -> str:
        return "\n".join(json.dumps(p) for p in payloads)

    def test_requires_target_id(self):
        r = self.client.post("/api/v1/decide/batch/jsonl", headers=self.headers, content="{}")
        self.assertEqual(r.status_code, 422)

    def test_imports_jsonl_and_returns_rows(self):
        body = self._jsonl({"amount": 1000}, {"amount": 2000}, {"amount": 3000})
        r = self.client.post(
            "/api/v1/decide/batch/jsonl?targetId={0}".format(self.policy["id"]),
            headers=self.headers,
            content=body,
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["rows"]), 3)
        self.assertIn("performance", data)
        for row in data["rows"]:
            self.assertEqual(row["result"]["policy_id"], self.policy["id"])

    def test_blank_lines_skipped_and_bad_line_reported(self):
        body = "\n".join([
            json.dumps({"amount": 1000}),
            "",  # blank -> skipped
            "{not valid json}",  # -> parse error, not a payload
            json.dumps({"amount": 2000}),
        ])
        r = self.client.post(
            "/api/v1/decide/batch/jsonl?targetId={0}".format(self.policy["id"]),
            headers=self.headers,
            content=body,
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 2, "two valid payloads")
        self.assertEqual(len(data["parseErrors"]), 1)
        self.assertEqual(data["parseErrors"][0]["line"], 3)

    def test_export_as_jsonl(self):
        body = self._jsonl({"amount": 1000}, {"amount": 2000})
        r = self.client.post(
            "/api/v1/decide/batch/jsonl?targetId={0}&format=jsonl".format(self.policy["id"]),
            headers=self.headers,
            content=body,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers.get("content-type", "").startswith("application/x-ndjson"))
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        lines = [line for line in r.text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            parsed = json.loads(line)  # each line is valid JSON
            self.assertIn("result", parsed)

    def test_matches_json_batch_path(self):
        payloads = [{"amount": 1000}, {"amount": 9999}]
        jsonl = self.client.post(
            "/api/v1/decide/batch/jsonl?targetId={0}".format(self.policy["id"]),
            headers=self.headers,
            content=self._jsonl(*payloads),
        ).json()
        js = self.client.post(
            "/api/v1/decide/batch",
            headers=self.headers,
            json={"targetType": "decide", "targetId": self.policy["id"], "payloads": payloads},
        ).json()
        self.assertEqual(
            [row["result"]["outcome"] for row in jsonl["rows"]],
            [row["result"]["outcome"] for row in js["rows"]],
        )

    def test_requires_auth(self):
        r = self.client.post(
            "/api/v1/decide/batch/jsonl?targetId={0}".format(self.policy["id"]),
            content="{}",
        )
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
