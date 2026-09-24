"""The typed response contract holds against real responses (plan 5.1).

`app/api_models.py` documents what the integration-critical endpoints return. A document like
that is only worth something if it cannot drift, so every test here takes a REAL response and
checks it two ways:

  * it validates against the model (types, required fields, nullability);
  * it carries no field the model does not declare. The models are open at runtime -- clients
    must ignore unknown fields -- so without this second check a field could be added to a
    handler and never reach the document, which is exactly how the API came to have 291
    undocumented responses.

Each endpoint is exercised on every path that produces it, because that is where the one real
divergence was: `rule_results` had one shape on the executor path and another on the FAST_DECIDE
path, under the same field name.

The last class checks the other half of the document -- which operations need credentials --
against the middleware, by sending every documented operation without any.
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, List
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel, TypeAdapter  # noqa: E402

import app.main as app_main  # noqa: E402
from app import api_models as m  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

FAST_POLICY = "policy_pl_underwriting"      # the one seeded policy FAST_DECIDE accepts
REVIEW_POLICY = "policy_sme_underwriting"   # pauses at a review gate


def undeclared_fields(value: Any, path: str = "$") -> List[str]:
    """Every field in a validated tree that its model does not declare."""
    found: List[str] = []
    if isinstance(value, m.ApiModel):
        if type(value).OPEN:
            return found
        for key in sorted((value.__pydantic_extra__ or {})):
            found.append(f"{path}.{key} ({type(value).__name__})")
        # __dict__, not getattr: reading a deprecated field through the attribute warns.
        for name in type(value).model_fields:
            found += undeclared_fields(value.__dict__.get(name), f"{path}.{name}")
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            found += undeclared_fields(value.__dict__.get(name), f"{path}.{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += undeclared_fields(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            found += undeclared_fields(item, f"{path}.{key}")
    return found


class ContractCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "contract.db"))
        app_main.storage = self.storage
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.tenant = self.storage.default_tenant_id
        self.headers = {"x-api-key": self.storage.default_api_key or ""}

    def conforms(self, schema: Any, body: Any) -> Any:
        """Validate `body` against `schema` and fail on any undeclared field."""
        try:
            validated = TypeAdapter(schema).validate_python(body)
        except Exception as error:  # noqa: BLE001 - the message is the useful part
            self.fail(f"response does not match {getattr(schema, '__name__', schema)}:\n{error}")
        extra = undeclared_fields(validated)
        self.assertEqual(extra, [], "fields the server sends and the contract does not declare")
        return validated

    def decide(self, policy_id: str, fast: str = "0", **extra: Any) -> dict:
        with mock.patch.dict(os.environ, {"FAST_DECIDE": fast}):
            response = self.client.post("/api/v1/decide", headers=self.headers,
                                        json={"policy_id": policy_id, "payload": {}, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class DecideContractTests(ContractCase):
    def test_every_seeded_policy_on_the_executor_path(self):
        for policy in self.storage.list_policies(tenant_id=self.tenant):
            with self.subTest(policy=policy["id"]):
                self.conforms(m.DecideResponse, self.decide(policy["id"]))

    def test_the_fast_path(self):
        from app.fast_decide import fast_path_eligible

        self.assertTrue(fast_path_eligible(self.storage, self.storage.get_policy(FAST_POLICY, tenant_id=self.tenant),
                                           self.tenant), "the fast path would silently not run")
        body = self.decide(FAST_POLICY, fast="1")
        self.conforms(m.DecideResponse, body)
        self.assertEqual((body["status"], body["execution_id"]), ("completed", None))

    def test_a_paused_decision_returns_a_handle_that_resolves(self):
        body = self.conforms(m.DecideResponse, self.decide(REVIEW_POLICY))
        self.assertEqual(body.status, "paused")
        self.assertIsNone(body.decision_id, "a paused decision has no Decision row yet")
        self.assertIsNotNone(body.execution_id, "a paused decision left the caller no handle")
        fetched = self.client.get(f"/api/v1/executions/{body.execution_id}", headers=self.headers)
        self.assertEqual(fetched.status_code, 200, "the execution_id names nothing")

    def test_a_completed_decision_names_no_execution(self):
        """The executor mints an execution id for every run and writes a row only when it has to;
        returning the minted one would hand out an id that names nothing."""
        body = self.decide(FAST_POLICY)
        self.assertEqual((body["status"], body["execution_id"]), ("completed", None))

    def test_batch_decide_on_both_paths(self):
        for policy_id in (FAST_POLICY, REVIEW_POLICY):
            with self.subTest(policy=policy_id):
                response = self.client.post("/api/v1/decide/batch", headers=self.headers,
                                            json={"policyId": policy_id, "payloads": [{}, {"x": 1}]})
                self.assertEqual(response.status_code, 200, response.text)
                self.conforms(m.BatchDecideResponse, response.json())


class DecisionRecordContractTests(ContractCase):
    def test_records_from_both_paths_list_and_fetch(self):
        slow = self.decide(FAST_POLICY)["decision_id"]
        fast = self.decide(FAST_POLICY, fast="1")["decision_id"]
        listed = self.client.get("/api/v1/audit/decisions", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.conforms(List[m.DecisionRecord], listed.json())
        for decision_id in (slow, fast):
            with self.subTest(decision=decision_id):
                one = self.client.get(f"/api/v1/audit/decisions/{decision_id}", headers=self.headers)
                self.conforms(m.DecisionRecord, one.json())

    def test_reason_codes_before_and_after_erasure(self):
        decision_id = self.decide(FAST_POLICY, user_id="subject-9")["decision_id"]
        before = self.client.get(f"/api/v1/decisions/{decision_id}/reason-codes", headers=self.headers)
        self.assertTrue(self.conforms(m.ReasonCodesResponse, before.json()).reason_codes)
        self.client.post("/api/v1/data-subjects/subject-9/erase", headers=self.headers,
                         json={"reason": "Art. 17 request verified"})
        after = self.client.get(f"/api/v1/decisions/{decision_id}/reason-codes", headers=self.headers)
        self.assertIsNotNone(self.conforms(m.ReasonCodesResponse, after.json()).erased_at)
        erased = self.client.get(f"/api/v1/audit/decisions/{decision_id}", headers=self.headers)
        self.conforms(m.DecisionRecord, erased.json())

    def test_replay(self):
        self.client.get("/sdk/v1/bundle", headers=self.headers)
        policy_id = self.storage.list_policies(status="prod", tenant_id=self.tenant)[0]["id"]
        decision_id = self.decide(policy_id)["decision_id"]
        response = self.client.post(f"/api/v1/decisions/{decision_id}/replay", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.conforms(m.ReplayResponse, response.json())

    def test_executions(self):
        self.decide(REVIEW_POLICY)
        response = self.client.get("/api/v1/executions", headers=self.headers)
        self.assertTrue(self.conforms(List[m.ExecutionSummary], response.json()))


class PlatformContractTests(ContractCase):
    def _basic(self) -> dict:
        issued = self.storage.generate_api_key_for_tenant(self.tenant)
        kid = self.storage.get_tenant_by_api_key(issued["plaintext"])["api_key"]["kid"]
        return {"authorization": "Basic " + base64.b64encode(f"{kid}:{issued['plaintext']}".encode()).decode()}

    def test_oauth_token_and_its_errors(self):
        ok = self.client.post("/api/v1/oauth/token", data={"grant_type": "client_credentials"}, headers=self._basic())
        self.assertEqual(ok.status_code, 200, ok.text)
        self.conforms(m.OAuthTokenResponse, ok.json())
        wrong_grant = self.client.post("/api/v1/oauth/token", data={"grant_type": "password"}, headers=self._basic())
        self.assertEqual(wrong_grant.status_code, 400)
        self.conforms(m.OAuthErrorResponse, wrong_grant.json())
        bad_client = self.client.post("/api/v1/oauth/token", data={"grant_type": "client_credentials"},
                                      headers={"authorization": "Basic " + base64.b64encode(b"nobody:nothing").decode()})
        self.assertEqual(bad_client.status_code, 401)
        self.conforms(m.OAuthErrorResponse, bad_client.json())

    def test_sdk_endpoints(self):
        self.conforms(m.SigningKeyResponse, self.client.get("/sdk/v1/signing-key", headers=self.headers).json())
        bundle = self.client.get("/sdk/v1/bundle", headers=self.headers)
        self.assertEqual(bundle.status_code, 200, bundle.text)
        self.conforms(m.SdkBundleResponse, bundle.json())
        decided = self.client.post("/sdk/v1/decide", headers=self.headers, json={"policyId": FAST_POLICY, "payload": {}})
        self.assertEqual(decided.status_code, 200, decided.text)
        self.conforms(m.SdkDecideResponse, decided.json())

    def test_data_subject_export_and_erasure(self):
        self.decide(FAST_POLICY, user_id="subject-1")
        self.decide(REVIEW_POLICY, user_id="subject-1")
        export = self.client.get("/api/v1/data-subjects/subject-1", headers=self.headers).json()
        self.assertEqual(self.conforms(m.DataSubjectExport, export).counts.decisions, 1)
        receipt = self.client.post("/api/v1/data-subjects/subject-1/erase", headers=self.headers,
                                   json={"reason": "Art. 17 request verified"})
        self.conforms(m.ErasureReceipt, receipt.json())

    def test_residency_with_every_kind_of_destination(self):
        self.storage.create_connector({"id": "bureau_x", "name": "B", "schema_paths": [], "sample_payload": {},
                                       "config": {"auth_type": "oauth2", "base_url": "https://api.bureau.example/v1",
                                                  "token_url": "https://auth.bureau.example/token",
                                                  "client_secret": "S3CRET", "fetch_at_decision": True}},
                                      tenant_id=self.tenant)
        self.storage.create_webhook_subscription({"name": "partner", "url": "https://hooks.partner.example/rm",
                                                  "events": ["*"], "secret": "whsec"}, tenant_id=self.tenant)
        with mock.patch.dict(os.environ, {"DECISION_ARCHIVE_SINK": "s3", "DECISION_ARCHIVE_S3_BUCKET": "rm-archive",
                                          "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092",
                                          "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel.internal:4318",
                                          "RULEMIND_DATA_REGION": "in-mumbai-1"}):
            report = self.client.get("/api/v1/data-residency", headers=self.headers).json()
        kinds = {item.kind for item in self.conforms(m.ResidencyReport, report).egress}
        self.assertTrue({"connector", "connector_token_endpoint", "webhook_subscription",
                         "decision_archive", "kafka", "tracing"} <= kinds, kinds)

    def test_liveness_and_readiness(self):
        for path in ("/health", "/api/v1/health"):
            self.conforms(m.HealthResponse, self.client.get(path).json())
        for path in ("/ready", "/health/ready"):
            self.conforms(m.ReadinessResponse, self.client.get(path).json())

    def test_errors_are_the_envelope(self):
        cases = {
            "unauthenticated": self.client.get("/api/v1/audit/decisions"),
            "not found": self.client.get("/api/v1/audit/decisions/nope", headers=self.headers),
            "unknown route": self.client.get("/api/v1/no-such-thing", headers=self.headers),
        }
        for name, response in cases.items():
            with self.subTest(name):
                self.assertGreaterEqual(response.status_code, 400)
                self.assertIsInstance(self.conforms(m.ErrorResponse, response.json()).error, str)
        invalid = self.client.post("/api/v1/decide", headers=self.headers, json={})
        self.assertEqual(invalid.status_code, 422)
        body = self.conforms(m.ValidationErrorResponse, invalid.json())
        self.assertEqual(body.error, "validation_error")
        self.assertIsInstance(body.detail[0], m.ValidationIssue)


class DocumentTests(unittest.TestCase):
    """The document says what the tests above check."""

    TYPED = {
        ("post", "/api/v1/decide"): "DecideResponse",
        ("post", "/api/v1/decide/batch"): "BatchDecideResponse",
        ("get", "/api/v1/audit/decisions"): "DecisionRecord",
        ("get", "/api/v1/audit/decisions/{decision_id}"): "DecisionRecord",
        ("get", "/api/v1/decisions/{decision_id}/reason-codes"): "ReasonCodesResponse",
        ("post", "/api/v1/decisions/{decision_id}/replay"): "ReplayResponse",
        ("get", "/api/v1/executions"): "ExecutionSummary",
        ("post", "/api/v1/oauth/token"): "OAuthTokenResponse",
        ("get", "/api/v1/data-subjects/{subject_id}"): "DataSubjectExport",
        ("post", "/api/v1/data-subjects/{subject_id}/erase"): "ErasureReceipt",
        ("get", "/api/v1/data-residency"): "ResidencyReport",
        ("get", "/sdk/v1/signing-key"): "SigningKeyResponse",
        ("get", "/sdk/v1/bundle"): "SdkBundleResponse",
        ("post", "/sdk/v1/decide"): "SdkDecideResponse",
        ("get", "/health"): "HealthResponse",
        ("get", "/ready"): "ReadinessResponse",
    }

    @classmethod
    def setUpClass(cls):
        cls.spec = app_main.app.openapi()

    def test_each_integration_endpoint_documents_its_model(self):
        for (method, path), model in self.TYPED.items():
            with self.subTest(f"{method.upper()} {path}"):
                schema = self.spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
                ref = schema.get("$ref") or (schema.get("items") or {}).get("$ref")
                self.assertEqual(ref, f"#/components/schemas/{model}", schema)

    def test_no_422_is_documented_in_fastapis_shape(self):
        """FastAPI documents 422 as {detail: [...]} with no `error`; the app never sends that."""
        self.assertNotIn("HTTPValidationError", self.spec["components"]["schemas"])

    def test_every_operation_documents_the_error_envelope_and_its_credentials(self):
        for path, item in self.spec["paths"].items():
            for method, operation in item.items():
                with self.subTest(f"{method.upper()} {path}"):
                    self.assertIn("4XX", operation["responses"])
                    self.assertIn("security", operation)
                    self.assertTrue(operation.get("tags"), "untagged operation")


class DocumentedCredentialsMatchTheMiddlewareTests(ContractCase):
    """Every operation, sent with no credentials. The middleware's refusal is a 401 whose `error`
    is `unauthorized`; an operation documented as needing credentials must get exactly that, and
    one documented as public must not. A document that says an endpoint is open when it is not
    breaks every generated client that follows it; one that says it is guarded when it is not
    hides an open endpoint."""

    def test_the_document_agrees_with_the_middleware(self):
        spec = app_main.app.openapi()
        # A handler that raises is a finding here, not a crash of the whole sweep.
        client = TestClient(app_main.app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        disagreements = []
        with mock.patch.dict(os.environ, {"AUTH_MODE": "enforced"}):
            for path, item in spec["paths"].items():
                concrete = path
                for name in [segment[1:-1] for segment in path.split("/") if segment.startswith("{")]:
                    concrete = concrete.replace("{" + name + "}", "contract-probe")
                for method, operation in item.items():
                    response = client.request(method.upper(), concrete)
                    try:
                        refused = response.status_code == 401 and response.json().get("error") == "unauthorized"
                    except ValueError:
                        refused = False
                    if refused != bool(operation["security"]):
                        disagreements.append(f"{method.upper()} {path}: documented "
                                             f"{'guarded' if operation['security'] else 'public'}, got "
                                             f"{response.status_code} {response.text[:80]}")
        self.assertEqual(disagreements, [])


if __name__ == "__main__":
    unittest.main()
