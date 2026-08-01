from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
SDK_PYTHON_ROOT = REPO_ROOT / "packages" / "sdk-python" / "src"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
if str(SDK_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_PYTHON_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")

import app.main as app_main
from app import cli as app_cli
from app.main import ConnectorUpdateRequest, RuleUpsertRequest, VariableUpsertRequest
from app.models import Tenant
from app.storage import Storage
from rulemind.client import RuleMindServerClient


class RuleMindPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.tempdir.name, "rulemind-test.db")
        app_main.storage = Storage(path=self.database_path)
        self.client = TestClient(app_main.app)
        self.default_headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def _rewind_bundle_queue(self, seconds: int = 10) -> None:
        with app_main.storage.connect() as session:
            tenant = session.get(Tenant, str(app_main.storage.default_tenant_id))
            self.assertIsNotNone(tenant)
            tenant.last_bundle_queued_at = datetime.utcnow() - timedelta(seconds=seconds)

    def _admin_cookie(self) -> dict[str, str]:
        response = self.client.post(
            "/api/admin/v1/auth/login",
            json={"email": app_main.storage.default_admin_email, "password": app_main.storage.default_admin_password},
        )
        self.assertEqual(response.status_code, 200)
        cookie = response.cookies.get("rulemind_admin_session")
        self.assertTrue(cookie)
        return {"rulemind_admin_session": cookie}

    def test_missing_invalid_and_revoked_api_keys_are_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/v1/connectors").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/connectors", headers={"x-api-key": "rm_live_invalid"}).status_code, 401)

        created = app_main.storage.generate_api_key_for_tenant(str(app_main.storage.default_tenant_id))
        self.assertTrue(app_main.storage.revoke_api_key(str(app_main.storage.default_tenant_id), created["kid"]))
        revoked = self.client.get("/api/v1/connectors", headers={"x-api-key": created["plaintext"]})
        self.assertEqual(revoked.status_code, 401)

    def test_tenant_runtime_queries_are_scoped_by_api_key(self) -> None:
        tenant_b = app_main.storage.create_tenant("Tenant B", plan="enterprise")
        key_b = app_main.storage.generate_api_key_for_tenant(tenant_b["id"])["plaintext"]
        app_main.storage.create_variable(
            {
                "id": "tenant_b_only",
                "name": "Tenant B Only",
                "category": "Custom",
                "source_id": "custom",
                "code": "def run(payload, context):\n    return 99\n",
                "status": "dev",
                "version": 1,
            },
            tenant_id=tenant_b["id"],
        )

        tenant_a_variables = self.client.get("/api/v1/variables", headers=self.default_headers)
        self.assertEqual(tenant_a_variables.status_code, 200)
        self.assertFalse(any(item["id"] == "tenant_b_only" for item in tenant_a_variables.json()))

        tenant_b_variables = self.client.get("/api/v1/variables", headers={"x-api-key": key_b})
        self.assertEqual(tenant_b_variables.status_code, 200)
        self.assertTrue(any(item["id"] == "tenant_b_only" for item in tenant_b_variables.json()))

    def test_bundle_recompilation_debounces_and_failures_preserve_latest(self) -> None:
        self.assertIsNone(app_main.storage.latest_bundle())

        first_response = self.client.post(
            "/api/v1/variables",
            headers=self.default_headers,
            json={
                "name": "Bundle One",
                "category": "Custom",
                "source_id": "custom",
                "code": "def run(payload, context):\n    return 1\n",
                "status": "prod",
            },
        )
        self.assertEqual(first_response.status_code, 200)
        first = app_main.storage.latest_bundle()
        self.assertIsNotNone(first)
        self.assertEqual(first["version"], 1)

        second_response = self.client.post(
            "/api/v1/variables",
            headers=self.default_headers,
            json={
                "name": "Bundle Two",
                "category": "Custom",
                "source_id": "custom",
                "code": "def run(payload, context):\n    return 2\n",
                "status": "prod",
            },
        )
        self.assertEqual(second_response.status_code, 200)
        second = app_main.storage.latest_bundle()
        self.assertIsNotNone(second)
        self.assertEqual(second["version"], 1)

        self._rewind_bundle_queue()
        updated = self.client.put(
            "/api/v1/variables/bundle_two",
            headers=self.default_headers,
            json={
                "name": "Bundle Two",
                "category": "Custom",
                "source_id": "custom",
                "code": "def run(payload, context):\n    return 22\n",
                "status": "prod",
            },
        )
        self.assertEqual(updated.status_code, 200)
        latest = app_main.storage.latest_bundle()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["version"], 2)

        self._rewind_bundle_queue()
        broken = self.client.put(
            "/api/v1/variables/bundle_one",
            headers=self.default_headers,
            json={
                "name": "Bundle One",
                "category": "Custom",
                "source_id": "custom",
                "code": "def broken(:\n    return 3\n",
                "status": "prod",
            },
        )
        self.assertEqual(broken.status_code, 200)
        latest = app_main.storage.latest_bundle()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["version"], 2)
        self.assertTrue(
            any(error["scope"] == "bundle" and error["stage"] == "compile" for error in app_main.storage.list_error_events())
        )

        response = self.client.get("/sdk/v1/bundle", headers={**self.default_headers, "x-bundle-version": "2"})
        self.assertEqual(response.status_code, 304)

    def test_server_only_variables_are_excluded_from_bundle(self) -> None:
        created = self.client.post(
            "/api/v1/variables",
            headers=self.default_headers,
            json={
                "name": "Needs Server",
                "category": "Custom",
                "source_id": "custom",
                "code": "def run(payload, context):\n    import math\n    return math.ceil(1.2)\n",
                "status": "prod",
            },
        )
        self.assertEqual(created.status_code, 200)
        bundle = app_main.storage.latest_bundle()
        self.assertIsNotNone(bundle)
        self.assertIn("needs_server", bundle["content"]["serverOnlyVariables"])
        self.assertFalse(any(item["id"] == "needs_server" for item in bundle["content"]["variables"]))

    def test_alembic_upgrade_applies_cleanly_to_empty_database(self) -> None:
        upgrade_path = os.path.join(self.tempdir.name, "alembic-smoke.db")
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "apps" / "python-executor" / "alembic"))
        config.set_main_option("sqlalchemy.url", "sqlite:///{0}".format(upgrade_path))
        os.environ["DATABASE_URL"] = "sqlite:///{0}".format(upgrade_path)
        try:
            command.upgrade(config, "head")
        finally:
            os.environ.pop("DATABASE_URL", None)
        self.assertTrue(os.path.exists(upgrade_path))

    def test_admin_tenant_management_requires_cookie_and_works(self) -> None:
        unauthorized = self.client.get("/api/admin/v1/tenants")
        self.assertEqual(unauthorized.status_code, 401)

        cookies = self._admin_cookie()
        created = self.client.post("/api/admin/v1/tenants", json={"name": "Admin Created", "plan": "standard"}, cookies=cookies)
        self.assertEqual(created.status_code, 200)
        tenant_id = created.json()["id"]

        listed = self.client.get("/api/admin/v1/tenants", cookies=cookies)
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(item["id"] == tenant_id for item in listed.json()))

        key_response = self.client.post(f"/api/admin/v1/tenants/{tenant_id}/keys", cookies=cookies)
        self.assertEqual(key_response.status_code, 200)
        self.assertTrue(key_response.json()["plaintext"].startswith("rm_live_"))

    def test_mobile_demo_access_and_manifest_are_available(self) -> None:
        response = self.client.post("/api/mobile/v1/auth/demo")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "demo")
        self.assertTrue(body["apiKey"].startswith("rm_live_"))
        self.assertIn("experienceManifest", body)
        manifest = body["experienceManifest"]
        self.assertEqual(manifest["locale"], "en-IN")
        self.assertEqual(manifest["shell"]["appName"], "RuleMind Experience Studio")
        self.assertEqual(manifest["design"]["themeId"], "rulemind_slate_steel")
        self.assertEqual(manifest["design"]["colors"]["primary"], "#005394")
        self.assertEqual(manifest["design"]["colors"]["surface"], "#F7FAFC")
        self.assertTrue(any(item["id"] == "travel_guard" for item in manifest["journeys"]))
        self.assertTrue(any(item["id"] == "instant_personal_loan" for item in manifest["journeys"]))
        self.assertTrue(any(item["id"] == "sme_underwriting" for item in manifest["journeys"]))
        self.assertEqual(sum(len(item["screens"]) for item in manifest["journeys"]), 25)
        self.assertTrue(any(item["id"] == "variables" for item in manifest["admin"]["entities"]))

        manifest_response = self.client.get("/sdk/v1/experience-manifest", headers=self.default_headers)
        self.assertEqual(manifest_response.status_code, 200)
        self.assertEqual(manifest_response.json()["tenantId"], str(app_main.storage.default_tenant_id))

    def test_mobile_admin_login_can_switch_tenants_and_fetch_profile(self) -> None:
        tenant_b = app_main.storage.create_tenant("Mobile Tenant", plan="enterprise")
        response = self.client.post(
            "/api/mobile/v1/auth/login",
            json={
                "email": app_main.storage.default_admin_email,
                "password": app_main.storage.default_admin_password,
                "tenantId": tenant_b["id"],
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "admin")
        self.assertTrue(body["accessToken"])
        self.assertEqual(body["tenant"]["id"], tenant_b["id"])

        auth_headers = {"Authorization": f"Bearer {body['accessToken']}"}
        me_response = self.client.get("/api/mobile/v1/auth/me", headers=auth_headers)
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["user"]["email"], app_main.storage.default_admin_email)
        self.assertTrue(any(item["id"] == tenant_b["id"] for item in me_response.json()["availableTenants"]))

        switched = self.client.post(
            f"/api/mobile/v1/tenants/{app_main.storage.default_tenant_id}/session",
            headers=auth_headers,
        )
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.json()["tenant"]["id"], str(app_main.storage.default_tenant_id))
        self.assertTrue(switched.json()["apiKey"].startswith("rm_live_"))

    def test_runtime_detail_endpoints_return_seeded_assets(self) -> None:
        connector = self.client.get("/api/v1/connectors/loan", headers=self.default_headers)
        self.assertEqual(connector.status_code, 200)
        self.assertEqual(connector.json()["id"], "loan")

        variable = self.client.get("/api/v1/variables/loan_bureau_score", headers=self.default_headers)
        self.assertEqual(variable.status_code, 200)
        self.assertEqual(variable.json()["id"], "loan_bureau_score")

        rule = self.client.get("/api/v1/rules/rule_loan_bureau_gate", headers=self.default_headers)
        self.assertEqual(rule.status_code, 200)
        self.assertEqual(rule.json()["id"], "rule_loan_bureau_gate")

        scorecard = self.client.get("/api/v1/scorecards/sc_loan_risk", headers=self.default_headers)
        self.assertEqual(scorecard.status_code, 200)
        self.assertEqual(scorecard.json()["id"], "sc_loan_risk")

        policy = self.client.get("/api/v1/policies/policy_instant_personal_loan", headers=self.default_headers)
        self.assertEqual(policy.status_code, 200)
        self.assertEqual(policy.json()["id"], "policy_instant_personal_loan")

    def test_rate_limit_standard_and_enterprise_plan_behavior(self) -> None:
        standard_tenant = app_main.storage.create_tenant("Standard Tenant", plan="standard")
        standard_key = app_main.storage.generate_api_key_for_tenant(standard_tenant["id"])["plaintext"]
        enterprise_tenant = app_main.storage.create_tenant("Enterprise Tenant", plan="enterprise")
        enterprise_key = app_main.storage.generate_api_key_for_tenant(enterprise_tenant["id"])["plaintext"]

        lookup = {
            standard_key: {
                "tenant": standard_tenant,
                "api_key": {"id": "standard-key"},
            },
            enterprise_key: {
                "tenant": enterprise_tenant,
                "api_key": {"id": "enterprise-key"},
            },
        }

        with patch.object(app_main.storage, "get_tenant_by_api_key", side_effect=lambda key: lookup.get(key)):
            standard_last = None
            for _ in range(1001):
                standard_last = self.client.get("/api/v1/connectors", headers={"x-api-key": standard_key})
            self.assertIsNotNone(standard_last)
            self.assertEqual(standard_last.status_code, 429)
            self.assertIn("Retry-After", standard_last.headers)

            enterprise_last = None
            for _ in range(1001):
                enterprise_last = self.client.get("/api/v1/connectors", headers={"x-api-key": enterprise_key})
            self.assertIsNotNone(enterprise_last)
            self.assertEqual(enterprise_last.status_code, 200)

    def test_v2_rule_round_trip_persists_nodes_and_tree(self) -> None:
        created = app_main.create_rule(
            RuleUpsertRequest(
                name="Round Trip Nested",
                ruleFormat="v2",
                tree={
                    "type": "group",
                    "logic": "AND",
                    "children": [
                        {"type": "condition", "variable": "bureau_score", "operator": ">=", "value": 700},
                        {"type": "condition", "variable": "avg_balance", "operator": ">=", "value": 20000},
                    ],
                    "onPass": "approve",
                    "onFail": "reject",
                },
            )
        )

        stored = app_main.storage.get_rule(created["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["rule_format"], "v2")
        self.assertIsInstance(stored["tree"], dict)
        self.assertGreaterEqual(len(stored["nodes"]), 2)

        outcome = app_main.test_rule(created["id"], app_main.TestPayloadRequest(payload={}))
        self.assertEqual(outcome["result"]["outcome"], "approve")

    def test_experiment_analytics_returns_expected_z_test(self) -> None:
        experiment = app_main.storage.create_or_update_experiment(
            {
                "id": "exp_ztest",
                "name": "Z Test",
                "status": "running",
                "variants": [{"id": "control", "weight": 50}, {"id": "treatment", "weight": 50}],
                "target_policy_id": "personal_loan_policy",
            }
        )
        for index in range(100):
            app_main.storage.add_decision(
                {
                    "policy_id": "personal_loan_policy",
                    "payload": {"id": "c-{0}".format(index)},
                    "computed_variables": {},
                    "rule_results": [],
                    "outcome": "approve" if index < 20 else "reject",
                    "latency_ms": 10,
                    "experiment_id": experiment["id"],
                    "experiment_variant": "control",
                }
            )
            app_main.storage.add_decision(
                {
                    "policy_id": "personal_loan_policy",
                    "payload": {"id": "t-{0}".format(index)},
                    "computed_variables": {},
                    "rule_results": [],
                    "outcome": "approve" if index < 40 else "reject",
                    "latency_ms": 12,
                    "experiment_id": experiment["id"],
                    "experiment_variant": "treatment",
                }
            )
        response = self.client.get(f"/api/v1/analytics/experiments/{experiment['id']}", headers=self.default_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["significance"]["significant"])
        self.assertAlmostEqual(data["significance"]["pValue"], 0.001565, places=3)

    def test_cli_create_admin_smoke(self) -> None:
        previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "sqlite:///{0}".format(self.database_path)
        try:
            exit_code = app_cli.main(["create-admin", "--email", "cli-admin@example.com", "--password", "secret123", "--name", "CLI Admin"])
        finally:
            if previous_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_database_url
        self.assertEqual(exit_code, 0)
        created = app_main.storage.get_platform_admin_by_email("cli-admin@example.com")
        self.assertIsNotNone(created)

    def test_sdk_python_client_sends_bundle_headers_and_handles_304(self) -> None:
        captured: dict[str, object] = {}

        class _Response:
            status = 304

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self) -> bytes:
                return b""

        def fake_urlopen(req, timeout=0):
            captured["headers"] = dict(req.header_items())
            captured["timeout"] = timeout
            return _Response()

        client = RuleMindServerClient("https://sdk.example.com", api_key="rm_live_test", sdk_version="4.1.0")
        with patch("rulemind.client.request.urlopen", fake_urlopen):
            bundle = client.get_bundle(bundle_version=7, client_public_key="public-key")

        self.assertIsNone(bundle)
        headers = {str(key).lower(): value for key, value in dict(captured["headers"]).items()}
        self.assertEqual(headers["x-api-key"], "rm_live_test")
        self.assertEqual(headers["x-sdk-version"], "4.1.0")
        self.assertEqual(headers["x-bundle-version"], "7")
        self.assertEqual(headers["x-client-public-key"], "public-key")


if __name__ == "__main__":
    unittest.main()
