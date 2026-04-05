from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")

import app.main as app_main
import app.sandbox as sandbox
from app.main import PolicyUpsertRequest, RuleUpsertRequest, ScorecardUpsertRequest, VariableUpsertRequest
from app.storage import Storage


class RuleMindSdkRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.tempdir.name, "rulemind-sdk-runtime.db")
        app_main.storage = Storage(path=self.database_path)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def _create_sdk_policy(self) -> str:
        variable = app_main.create_variable(
            VariableUpsertRequest(
                name="SDK Bureau Score",
                category="Custom",
                source_id="bureau",
                code="def run(payload, context):\n    return payload.get('score', 0)\n",
                status="dev",
            )
        )
        rule = app_main.create_rule(
            RuleUpsertRequest(
                name="SDK Bureau Threshold",
                nodes=[
                    {"id": "c1", "type": "condition", "variable": variable["id"], "operator": ">=", "value": "700"},
                    {"id": "a1", "type": "approve", "label": "Approve"},
                ],
                status="dev",
            )
        )
        scorecard = app_main.create_scorecard(
            ScorecardUpsertRequest(
                name="SDK Risk Scorecard",
                base_score=300,
                max_score=900,
                bins=[{"variable_id": variable["id"], "ranges": [{"min": 700, "max": 750, "points": 275}]}],
                status="dev",
            )
        )
        policy = app_main.create_policy(
            PolicyUpsertRequest(
                name="SDK Decision Policy",
                steps=[
                    {"type": "rule", "id": "rule_step", "ref_id": rule["id"], "label": "Credit Eligibility"},
                    {"type": "scorecard", "id": "score_step", "ref_id": scorecard["id"], "label": "Credit Risk Scoring"},
                    {"type": "outcome", "id": "decision_step", "config": {"outcome": "approve"}},
                ],
                defaultOutcome="review",
                status="dev",
            )
        )
        return policy["id"]

    def _create_sdk_review_policy(self) -> str:
        policy = app_main.create_policy(
            PolicyUpsertRequest(
                name="SDK Review Policy",
                steps=[
                    {"type": "outcome", "id": "seed_review", "config": {"outcome": "review"}},
                    {
                        "type": "review_gate",
                        "id": "review_step",
                        "name": "Manual Review",
                        "config": {
                            "assignTo": "mobile_review",
                            "requiredFields": ["approved_amount"],
                            "condition": "outcome == 'review'",
                        },
                    },
                    {"type": "outcome", "id": "decision_step", "config": {"outcome": "approve"}},
                ],
                defaultOutcome="review",
                status="dev",
            )
        )
        return policy["id"]

    def test_execute_variable_falls_back_inline_when_pool_startup_fails(self) -> None:
        code = "def run(payload, context):\n    return payload.get('bureau', {}).get('score', 0)\n"
        with patch("app.sandbox._get_pool", side_effect=PermissionError("pool blocked")):
            result = sandbox.execute_variable(code, {"bureau": {"score": 720}})
        self.assertEqual(result["value"], 720)
        self.assertEqual(result["sandbox_fallback"], "inline")
        self.assertIn("pool blocked", result["sandbox_error"])

    def test_webhook_runtime_permission_errors_return_server_failures(self) -> None:
        webhook = self.client.post(
            "/api/v1/webhooks",
            headers=self.headers,
            json={"policy_id": self._create_sdk_policy(), "payload_mapping": {}},
        )
        self.assertEqual(webhook.status_code, 200)
        webhook_id = webhook.json()["id"]

        with patch("app.main.trigger_webhook", side_effect=PermissionError("sandbox unavailable")):
            response = self.client.post(f"/api/v1/webhooks/{webhook_id}", json={"application_id": "app-401"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Webhook execution failed.")
        self.assertTrue(
            any(event["scope"] == "webhook" and event["stage"] == "execute" for event in app_main.storage.list_error_events())
        )

    def test_sdk_decide_returns_normalized_contract(self) -> None:
        policy_id = self._create_sdk_policy()
        response = self.client.post(
            "/sdk/v1/decide",
            headers=self.headers,
            json={
                "policyId": policy_id,
                "requestId": "req-sdk-1",
                "userId": "user-sdk-1",
                "payload": {"bureau": {"score": 720}},
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["requestId"], "req-sdk-1")
        self.assertTrue(body["executionId"])
        self.assertEqual(body["ruleResults"][0]["ruleId"], body["ruleResults"][0]["rule_id"])
        self.assertEqual(body["ruleResults"][0]["ruleId"], "sdk_bureau_threshold")
        self.assertEqual(body["scorecardResults"]["sdk_risk_scorecard"]["score"], 575)
        self.assertEqual(body["auditSummary"]["traceSteps"], 3)
        self.assertIn("rules", body["explainability"])
        self.assertIn("trace", body["explainability"])

    def test_sdk_execution_sync_round_trips_and_persists_decisions(self) -> None:
        policy_id = self._create_sdk_policy()
        response = self.client.post(
            "/sdk/v1/executions/sync",
            headers=self.headers,
            json={
                "executionId": "exec_sync_1",
                "policyId": policy_id,
                "payload": {"bureau": {"score": 720}},
                "userId": "user-sync-1",
                "outcome": "approve",
                "status": "completed",
                "variables": {"bureau_score": 720},
                "ruleResults": [{"ruleId": "sdk_bureau_threshold", "passed": True, "outcome": "approve"}],
                "scorecardResults": {"sdk_risk_scorecard": {"score": 575, "base_score": 300}},
                "actionResults": [
                    {
                        "operationId": "op_1",
                        "stepId": "notify_step",
                        "actionName": "Notify",
                        "url": "https://example.test/hooks",
                        "method": "POST",
                        "requestBody": {"application_id": "app-1"},
                        "responseStatus": 202,
                        "responseBody": '{"ok":true}',
                        "latencyMs": 8,
                        "success": True,
                    }
                ],
                "trace": [{"step": {"id": "decision_step", "type": "outcome"}, "result": {"outcome": "approve"}}],
                "latencyMs": 11,
                "requestId": "req-sync-1",
                "source": "edge_bundle",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["executionId"], "exec_sync_1")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["actionResults"][0]["responseStatus"], 202)

        fetched = self.client.get("/sdk/v1/executions/exec_sync_1", headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["requestId"], "req-sync-1")
        self.assertEqual(fetched.json()["ruleResults"][0]["ruleId"], "sdk_bureau_threshold")

        decisions = app_main.storage.list_decisions()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["outcome"], "approve")
        self.assertEqual(decisions[0]["source"], "edge_bundle")

        action_logs = app_main.storage.list_action_logs(execution_id="exec_sync_1")
        self.assertEqual(len(action_logs), 1)
        self.assertEqual(action_logs[0]["response_status"], 202)

    def test_sdk_resume_execution_continues_paused_review_flows(self) -> None:
        policy_id = self._create_sdk_review_policy()
        synced = self.client.post(
            "/sdk/v1/executions/sync",
            headers=self.headers,
            json={
                "executionId": "exec_review_1",
                "policyId": policy_id,
                "payload": {"application_id": "app-77"},
                "userId": "user-review-1",
                "outcome": "review",
                "status": "paused",
                "trace": [
                    {"step": {"id": "seed_review", "type": "outcome"}, "result": {"outcome": "review"}},
                    {"step": {"id": "review_step", "type": "review_gate"}, "result": {"paused": True}},
                ],
                "reviewTask": {
                    "id": "sdk_review_1",
                    "step_id": "review_step",
                    "queue": "mobile_review",
                    "status": "pending",
                    "required_fields": ["approved_amount"],
                },
                "latencyMs": 3,
                "requestId": "req-review-1",
                "source": "sdk_edge",
            },
        )
        self.assertEqual(synced.status_code, 200)
        self.assertEqual(synced.json()["status"], "paused")

        resumed = self.client.post(
            "/sdk/v1/executions/exec_review_1/resume",
            headers=self.headers,
            json={"decision": "approve", "reviewerId": "reviewer-1", "response": {"approved_amount": 50000}},
        )
        self.assertEqual(resumed.status_code, 200)
        body = resumed.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["outcome"], "approve")
        self.assertTrue(body["reviewTask"])
        self.assertEqual(body["reviewTask"]["status"], "approved")

    def test_seeded_policies_do_not_override_rejects_with_final_outcomes(self) -> None:
        response = self.client.post(
            "/api/v1/policies/policy_instant_personal_loan/execute",
            headers=self.headers,
            json={
                "payload": {
                    "loan": {
                        "bureau_score": 730,
                        "avg_balance_inr": 52000,
                        "dti_ratio": 0.62,
                        "pan_verified_flag": 0,
                        "geo_consistency_flag": 0,
                        "liveness_score": 82.0,
                    }
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["outcome"], "reject")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(item.get("result", {}).get("outcome") == "reject" for item in result["trace"]))
        self.assertTrue(any(item.get("skipped") and "Condition not met" in item.get("reason", "") for item in result["trace"]))

    def test_seeded_manifest_scenarios_match_expected_outcomes_and_statuses(self) -> None:
        manifest_response = self.client.post("/api/mobile/v1/auth/demo")
        self.assertEqual(manifest_response.status_code, 200)
        manifest = manifest_response.json()["experienceManifest"]
        journeys = {item["id"]: item for item in manifest["journeys"]}
        observed_step_types: set[str] = set()

        for scenario in manifest["scenarios"]:
            journey = journeys[scenario["journeyId"]]
            response = self.client.post(
                "/sdk/v1/decide",
                headers=self.headers,
                json={
                    "policyId": journey["policyId"],
                    "requestId": f"seeded-{scenario['id']}",
                    "userId": "seeded-runtime-test",
                    "payload": scenario["payload"],
                },
            )
            self.assertEqual(response.status_code, 200, scenario["id"])
            body = response.json()
            self.assertEqual(body["outcome"], scenario["expectedOutcome"], scenario["id"])
            self.assertEqual(body["status"], scenario["expectedStatus"], scenario["id"])
            self.assertTrue(body["executionId"], scenario["id"])
            self.assertGreaterEqual(len(body["trace"]), 1, scenario["id"])
            for item in body["trace"]:
                step_type = (item.get("step") or {}).get("type")
                if isinstance(step_type, str) and step_type:
                    observed_step_types.add(step_type)
            if scenario["expectedStatus"] == "paused":
                self.assertTrue(body["reviewTask"], scenario["id"])

        self.assertTrue({"rule", "scorecard", "action", "review_gate", "outcome"}.issubset(observed_step_types))

    def test_seeded_review_scenarios_resume_to_completed_approvals(self) -> None:
        manifest = self.client.post("/api/mobile/v1/auth/demo").json()["experienceManifest"]
        journeys = {item["id"]: item for item in manifest["journeys"]}
        paused_scenarios = [item for item in manifest["scenarios"] if item["expectedStatus"] == "paused"]

        for scenario in paused_scenarios:
            journey = journeys[scenario["journeyId"]]
            response = self.client.post(
                "/sdk/v1/decide",
                headers=self.headers,
                json={
                    "policyId": journey["policyId"],
                    "requestId": f"resume-{scenario['id']}",
                    "userId": "seeded-resume-test",
                    "payload": scenario["payload"],
                },
            )
            self.assertEqual(response.status_code, 200, scenario["id"])
            paused = response.json()
            self.assertEqual(paused["status"], "paused", scenario["id"])
            resume_response = self.client.post(
                f"/sdk/v1/executions/{paused['executionId']}/resume",
                headers=self.headers,
                json={
                    "decision": "approve",
                    "reviewerId": "qa-mobile",
                    "response": {
                        "medical_clearance_note": "Cleared by automation",
                        "approved_amount_inr": 50000,
                        "underwriter_note": "Approved by automation",
                    },
                },
            )
            self.assertEqual(resume_response.status_code, 200, scenario["id"])
            resumed = resume_response.json()
            self.assertEqual(resumed["status"], "completed", scenario["id"])
            self.assertEqual(resumed["outcome"], "approve", scenario["id"])
            self.assertTrue(resumed["reviewTask"], scenario["id"])
            self.assertIn(resumed["reviewTask"]["status"], {"approved", "completed"})


if __name__ == "__main__":
    unittest.main()
