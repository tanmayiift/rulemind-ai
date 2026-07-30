from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")

import app.main as app_main
from app.auth import hmac_signature
from app.main import PolicyUpsertRequest
from app.storage import Storage


class _FakeHttpResponse:
    def __init__(self, status_code: int = 200, text: str = '{"ok":true}') -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP {0}".format(self.status_code))

    def json(self):
        import json

        return json.loads(self.text)


class _FakeAsyncClient:
    calls: list[dict[str, object]] = []

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return _FakeHttpResponse(201, '{"status":"created"}')

    async def get(self, url: str, headers=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return _FakeHttpResponse(200, '{"status":"ok"}')

    async def put(self, url: str, json=None, headers=None):
        self.calls.append({"method": "PUT", "url": url, "json": json, "headers": headers})
        return _FakeHttpResponse(200, '{"status":"ok"}')

    async def patch(self, url: str, json=None, headers=None):
        self.calls.append({"method": "PATCH", "url": url, "json": json, "headers": headers})
        return _FakeHttpResponse(200, '{"status":"ok"}')


class _FakeSchedulerClient:
    calls: list[dict[str, object]] = []

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, headers=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return _FakeHttpResponse(200, '[{"id":"g1"},{"id":"g2"}]')

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return _FakeHttpResponse(200, '{"items":[{"id":"p1"},{"id":"p2"}]}')


class RuleMindWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.tempdir.name, "rulemind-workflow.db")
        app_main.storage = Storage(path=self.database_path)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()
        _FakeAsyncClient.calls = []
        _FakeSchedulerClient.calls = []

    def _create_review_policy(self) -> str:
        policy = app_main.create_policy(
            PolicyUpsertRequest(
                name="Webhook Review Flow",
                steps=[
                    {"type": "outcome", "label": "review"},
                    {
                        "type": "review_gate",
                        "id": "rg1",
                        "name": "Manual Review",
                        "config": {
                            "assignTo": "underwriting_queue",
                            "timeoutHours": 48,
                            "onTimeout": "reject",
                            "requiredFields": ["approved_amount"],
                            "condition": "outcome == 'review'",
                        },
                    },
                    {
                        "type": "action",
                        "id": "a1",
                        "name": "Notify",
                        "config": {
                            "condition": "outcome == 'approve'",
                            "url": "https://client.example/api/notify",
                            "method": "POST",
                            "bodyTemplate": {
                                "application_id": "{{payload.application_id}}",
                                "amount": "{{review.approved_amount}}",
                                "outcome": "{{outcome}}",
                            },
                            "onFailure": "abort",
                        },
                    },
                ],
                status="dev",
            )
        )
        return policy["id"]

    def test_webhook_hmac_validation(self) -> None:
        policy_id = self._create_review_policy()
        webhook = self.client.post(
            "/api/v1/webhooks",
            headers=self.headers,
            json={"policy_id": policy_id, "secret": "super-secret", "payload_mapping": {}},
        )
        self.assertEqual(webhook.status_code, 200)
        webhook_id = webhook.json()["id"]

        bad = self.client.post(f"/api/v1/webhooks/{webhook_id}", json={"application_id": "app-1"})
        self.assertEqual(bad.status_code, 401)

        body = b'{"application_id":"app-1"}'
        signature = hmac_signature("super-secret", body)
        good = self.client.post(
            f"/api/v1/webhooks/{webhook_id}",
            data=body,
            headers={"content-type": "application/json", "x-webhook-signature": signature},
        )
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["status"], "paused")

    def test_webhook_review_resume_executes_action_and_persists_trace(self) -> None:
        policy_id = self._create_review_policy()
        webhook = self.client.post("/api/v1/webhooks", headers=self.headers, json={"policy_id": policy_id, "secret": "wh-secret", "payload_mapping": {}})
        self.assertEqual(webhook.status_code, 200)
        webhook_id = webhook.json()["id"]

        body = b'{"application_id":"app-42","user_id":"user-42"}'  # compact + sorted (matches server signing)
        paused = self.client.post(f"/api/v1/webhooks/{webhook_id}", data=body,
                                  headers={"content-type": "application/json", "x-webhook-signature": hmac_signature("wh-secret", body)})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "paused")

        reviews = self.client.get("/api/v1/reviews", headers=self.headers)
        self.assertEqual(reviews.status_code, 200)
        self.assertEqual(len(reviews.json()), 1)
        task = reviews.json()[0]
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["queue"], "underwriting_queue")

        with patch("app.executor.httpx.AsyncClient", _FakeAsyncClient):
            decided = self.client.post(
                f"/api/v1/reviews/{task['id']}/decide",
                headers=self.headers,
                json={"decision": "approve", "reviewer_id": "reviewer-1", "response": {"approved_amount": 50000}},
            )
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["status"], "completed")
        self.assertEqual(decided.json()["outcome"], "approve")

        executions = app_main.storage.list_decisions()
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["source"], "review")
        self.assertEqual(executions[0]["outcome"], "approve")
        self.assertGreaterEqual(len(executions[0]["trace"]), 3)

        action_logs = app_main.storage.list_action_logs(execution_id=decided.json()["executionId"])
        self.assertEqual(len(action_logs), 1)
        self.assertEqual(action_logs[0]["response_status"], 201)
        self.assertEqual(action_logs[0]["request_body"]["amount"], "50000")

        execution = app_main.storage.get_workflow_execution(decided.json()["executionId"])
        self.assertIsNotNone(execution)
        self.assertEqual(execution["status"], "completed")

    def test_action_logs_redact_resolved_secret_values(self) -> None:
        app_main.storage.update_connector("custom", {"config": {"DISBURSE_TOKEN": "super-secret-token"}})
        policy = app_main.create_policy(
            PolicyUpsertRequest(
                name="Secret Action Policy",
                steps=[
                    {"type": "outcome", "label": "approve"},
                    {
                        "type": "action",
                        "id": "a_secret",
                        "name": "Send Secret",
                        "config": {
                            "url": "https://client.example/api/action",
                            "method": "POST",
                            "bodyTemplate": {
                                "authorization": "{{secrets.DISBURSE_TOKEN}}",
                                "nested": {"header": "Bearer {{secrets.DISBURSE_TOKEN}}"},
                            },
                            "onFailure": "abort",
                        },
                    },
                ],
                status="dev",
            )
        )

        with patch("app.executor.httpx.AsyncClient", _FakeAsyncClient):
            result = app_main.test_policy_entity(policy, {"application_id": "app-1"})

        logs = app_main.storage.list_action_logs(execution_id=result["result"]["execution_id"])
        self.assertEqual(len(logs), 1)
        request_body = logs[0]["request_body"]
        serialized = str(request_body)
        self.assertNotIn("super-secret-token", serialized)
        self.assertEqual(request_body["authorization"], "***REDACTED***")
        self.assertIn("***REDACTED***", request_body["nested"]["header"])

    def test_http_pull_schedule_supports_get_and_post(self) -> None:
        policy = app_main.create_policy(
            PolicyUpsertRequest(
                name="Cron Policy",
                steps=[{"type": "outcome", "label": "approve"}],
                status="dev",
            )
        )
        get_schedule = self.client.post(
            "/api/v1/schedules",
            headers=self.headers,
            json={
                "policy_id": policy["id"],
                "cron_expression": "0 */6 * * *",
                "is_active": True,
                "payload_source": {"type": "http_pull", "url": "https://source.example/items", "method": "GET"},
                "config": {"batchSize": 10, "concurrency": 2},
            },
        )
        self.assertEqual(get_schedule.status_code, 200)
        post_schedule = self.client.post(
            "/api/v1/schedules",
            headers=self.headers,
            json={
                "policy_id": policy["id"],
                "cron_expression": "0 */6 * * *",
                "is_active": True,
                "payload_source": {
                    "type": "http_pull",
                    "url": "https://source.example/items/search",
                    "method": "POST",
                    "headers": {"x-client": "rulemind"},
                    "body": {"segment": "new"},
                },
                "config": {"batchSize": 10, "concurrency": 2},
            },
        )
        self.assertEqual(post_schedule.status_code, 200)

        with patch("app.scheduler.httpx.AsyncClient", _FakeSchedulerClient):
            get_run = self.client.post(f"/api/v1/schedules/{get_schedule.json()['id']}/run-now", headers=self.headers)
            post_run = self.client.post(f"/api/v1/schedules/{post_schedule.json()['id']}/run-now", headers=self.headers)

        self.assertEqual(get_run.status_code, 200)
        self.assertEqual(post_run.status_code, 200)
        self.assertTrue(any(call["method"] == "GET" for call in _FakeSchedulerClient.calls))
        self.assertTrue(any(call["method"] == "POST" for call in _FakeSchedulerClient.calls))

    def test_review_timeout_worker_resumes_execution(self) -> None:
        policy_id = self._create_review_policy()
        webhook = self.client.post("/api/v1/webhooks", headers=self.headers, json={"policy_id": policy_id, "secret": "wh-secret", "payload_mapping": {}})
        self.assertEqual(webhook.status_code, 200)
        body = b'{"application_id":"app-timeout"}'
        paused = self.client.post(f"/api/v1/webhooks/{webhook.json()['id']}", data=body,
                                  headers={"content-type": "application/json", "x-webhook-signature": hmac_signature("wh-secret", body)})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "paused")

        tasks = app_main.storage.list_review_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        app_main.storage.update_review_task(
            task["id"],
            {"timeout_at": datetime.utcnow() - timedelta(minutes=1)},
        )

        from app.scheduler import check_review_timeouts

        asyncio.run(check_review_timeouts(app_main.storage))

        updated_task = app_main.storage.get_review_task(task["id"])
        self.assertIsNotNone(updated_task)
        self.assertEqual(updated_task["status"], "timed_out")
        execution = app_main.storage.get_workflow_execution(paused.json()["executionId"])
        self.assertIsNotNone(execution)
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(execution["context"]["outcome"], "reject")


if __name__ == "__main__":
    unittest.main()
