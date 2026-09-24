"""Named-provider connector templates (plan 6.1), end to end.

Each template is installed through the API and then driven through a REAL decision
(`PolicyExecutor.execute`) against a local server that implements the provider's DOCUMENTED
contract -- the response bodies are the provider's own published examples, and the sandbox
behaviour (Setu's test PANs, FinBox's progress states, Equifax's hit codes and error bodies) is
the documented behaviour. So these tests are evidence that each template matches the published
contract. They are NOT evidence that the provider still honours it; that needs the provider's
sandbox and credentials the provider issues.

The egress guard refuses loopback, correctly, so `build_pinned_request` is patched to route the
template's real https URL to the local server. Everything else -- auth headers, the request body,
the OAuth2 token exchange, response checks, error mapping, retries, the breaker, the variables --
runs for real.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock
from urllib.parse import parse_qs, urlparse

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import circuit_breaker, connector_fetch, connector_templates, fast_decide, oauth2_client  # noqa: E402
from app.egress import PinnedRequest  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

LIVE_SOURCE = "webhook"

Handler = Callable[[str, str, Dict[str, str], bytes], Tuple[int, Any]]


class _Provider:
    """A local HTTP server. `handler(method, path, headers, body) -> (status, json_body)`."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: List[Dict[str, Any]] = []
        provider = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _serve(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                headers = {k.lower(): v for k, v in self.headers.items()}
                provider.requests.append({"method": self.command, "path": self.path,
                                          "headers": headers, "body": raw.decode()})
                status, body = provider.handler(self.command, self.path, headers, raw)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address
        self.origin = "http://{0}:{1}".format(host, port)

    def pin(self, url: str) -> PinnedRequest:
        """Route the template's real https URL to this server, keeping path and query."""
        parsed = urlparse(url)
        local = self.origin + parsed.path + (("?" + parsed.query) if parsed.query else "")
        netloc = urlparse(self.origin).netloc
        return PinnedRequest(url=local, host_header=netloc, sni_hostname="127.0.0.1", original_url=url)

    def to(self, path_prefix: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["path"].startswith(path_prefix)]


class _TemplateTest(unittest.TestCase):
    TEMPLATE = ""
    INSTALL: Dict[str, Any] = {}

    def handler(self, method: str, path: str, headers: Dict[str, str], body: bytes) -> Tuple[int, Any]:
        raise NotImplementedError

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "templates.db"))
        app_main.storage = self.storage
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.tenant_id = self.storage.default_tenant_id
        self.key = {"x-api-key": self.storage.generate_api_key_for_tenant(self.tenant_id)["plaintext"]}
        connector_fetch.reset_cache()
        circuit_breaker.reset_all()
        oauth2_client.reset()
        self.addCleanup(oauth2_client.reset)
        self.provider = _Provider(lambda *a: self.handler(*a))
        self.addCleanup(self.provider.server.shutdown)
        for target in ("app.connector_fetch.build_pinned_request", "app.oauth2_client.build_pinned_request"):
            patcher = mock.patch(target, side_effect=self.provider.pin)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, **overrides) -> Dict[str, Any]:
        body = {**copy.deepcopy(self.INSTALL), **overrides}
        response = self.client.post("/api/v1/connector-templates/{0}/install".format(self.TEMPLATE),
                                    json=body, headers=self.key)
        self.assertEqual(response.status_code, 200, response.text)
        fast_decide.invalidate(self.tenant_id)
        return response.json()

    def decide(self, custom: Dict[str, Any], connector_id: Optional[str] = None):
        cid = connector_id or connector_templates.TEMPLATES[self.TEMPLATE]["default_connector_id"]
        policy = {"id": "tpl_policy", "name": "Template policy", "status": "prod", "defaultOutcome": "review",
                  "steps": [{"id": "s_conn", "type": "connector", "ref_id": cid},
                            {"id": "s_out", "type": "outcome", "ref_id": "approve"}]}
        run = PolicyExecutor(self.storage).execute(policy, {"custom": custom}, self.tenant_id, source=LIVE_SOURCE)
        return asyncio.run(run)

    def fetch_entry(self, ctx) -> Dict[str, Any]:
        self.assertEqual(len(ctx.source_fetches), 1, ctx.source_fetches)
        return ctx.source_fetches[0]


# ── Setu PAN ────────────────────────────────────────────────────────────────────────────────
# Setu's documented sandbox: ABCDE1234A valid, ABCDE1234B invalid, anything else 404; consent
# other than Y/y is 400 consent_not_given. Bodies are Setu's published examples.
SETU_VALID = {"data": {"aadhaar_seeding_status": "LINKED", "category": "Individual", "full_name": "John Doe"},
              "message": "PAN is valid", "verification": "SUCCESS", "traceId": "1-6346a91a-620cf6cc4f68d2e30316881e"}
SETU_INVALID = {"verification": "failed", "message": "PAN is invalid"}
SETU_NOT_FOUND = {"message": "PAN not found.", "verification": "failed"}
SETU_NO_CONSENT = {"error": {"code": "consent_not_given", "detail": "Consent is required and should be Y/y.",
                             "traceId": "7097e53a-ba29-48a2-983d-878433b4f33e"}}


class SetuPanTemplateTest(_TemplateTest):
    TEMPLATE = "setu_pan_verification"
    INSTALL = {"environment": "sandbox",
               "settings": {"client_id": "setu-client", "product_instance_id": "setu-instance"},
               "secrets": {"client_secret": "SETU-SECRET-VALUE"}}
    override: Optional[Tuple[int, Any]] = None

    def handler(self, method, path, headers, body):
        if self.override:
            return self.override
        if path != "/api/verify/pan" or method != "POST":
            return 404, {"message": "no route"}
        request = json.loads(body or b"{}")
        if str(request.get("consent")) not in ("Y", "y"):
            return 400, SETU_NO_CONSENT
        return {"ABCDE1234A": (200, SETU_VALID), "ABCDE1234B": (200, SETU_INVALID)}.get(
            request.get("pan"), (404, SETU_NOT_FOUND))

    def test_a_valid_pan_is_verified_with_the_documented_envelope(self):
        self.install()
        ctx = self.decide({"pan": "ABCDE1234A", "pan_consent": "Y"})
        self.assertEqual(ctx.outcome, "approve")
        self.assertEqual(ctx.payload["setu_pan"]["full_name"], "John Doe")
        self.assertIs(ctx.variables["setu_pan_pan_verified"], True,
                      "upper-case SUCCESS (the OpenAPI example) must count as verified")
        self.assertIs(ctx.variables["setu_pan_aadhaar_linked"], True)
        sent = self.provider.to("/api/verify/pan")[0]
        self.assertEqual(sent["headers"]["x-client-id"], "setu-client")
        self.assertEqual(sent["headers"]["x-client-secret"], "SETU-SECRET-VALUE")
        self.assertEqual(sent["headers"]["x-product-instance-id"], "setu-instance")
        self.assertEqual(json.loads(sent["body"]), {
            "pan": "ABCDE1234A", "consent": "Y",
            "reason": "Identity verification for a credit application"})

    def test_an_invalid_pan_is_an_answer_not_a_failure(self):
        """200 verification "failed" -- the policy decides what an invalid PAN means."""
        self.install()
        ctx = self.decide({"pan": "ABCDE1234B", "pan_consent": "Y"})
        self.assertEqual(self.fetch_entry(ctx)["status"], "ok")
        self.assertIs(ctx.variables["setu_pan_pan_verified"], False)

    def test_an_unknown_pan_is_a_named_failure_that_goes_to_review(self):
        self.install()
        ctx = self.decide({"pan": "ZZZZZ9999Z", "pan_consent": "Y"})
        entry = self.fetch_entry(ctx)
        self.assertEqual(entry["outcome"], "setu_pan_not_found")
        self.assertIn("PAN not found.", entry["error"], "the provider's own message rides along")
        self.assertEqual(ctx.outcome, "review")

    def test_missing_consent_carries_setus_error_code(self):
        self.install()
        ctx = self.decide({"pan": "ABCDE1234A", "pan_consent": "N"})
        entry = self.fetch_entry(ctx)
        self.assertEqual(entry["outcome"], "setu_bad_request")
        self.assertIn("consent_not_given", entry["error"])

    def test_a_missing_input_is_named_and_nothing_is_sent(self):
        self.install()
        ctx = self.decide({"pan_consent": "Y"})
        entry = self.fetch_entry(ctx)
        self.assertEqual(entry["outcome"], "missing_input")
        self.assertIn("payload.custom.pan", entry["error"])
        self.assertEqual(self.provider.requests, [], "a request went out without the PAN")
        self.assertEqual(ctx.outcome, "review")

    def test_a_malformed_input_is_refused_without_echoing_it(self):
        self.install()
        ctx = self.decide({"pan": "../../admin", "pan_consent": "Y"})
        entry = self.fetch_entry(ctx)
        self.assertEqual(entry["outcome"], "invalid_input")
        self.assertNotIn("admin", entry["error"], "the reason names the field, never the value")
        self.assertEqual(self.provider.requests, [])

    def test_an_undocumented_verification_value_is_not_data(self):
        self.install()
        self.override = (200, {"verification": "PENDING", "message": "try later"})
        ctx = self.decide({"pan": "ABCDE1234A", "pan_consent": "Y"})
        self.assertEqual(self.fetch_entry(ctx)["outcome"], "setu_unexpected_verification_value")
        self.assertEqual(ctx.outcome, "review")

    def test_applicants_with_bad_pans_do_not_open_the_breaker(self):
        """Each 404 is Setu answering about one applicant. Counted as provider failures, five
        typo'd PANs opened the breaker and sent every other applicant to review."""
        self.install()
        for _ in range(8):
            self.decide({"pan": "ZZZZZ9999Z", "pan_consent": "Y"})
        ctx = self.decide({"pan": "ABCDE1234A", "pan_consent": "Y"})
        self.assertEqual(self.fetch_entry(ctx)["status"], "ok", self.fetch_entry(ctx))

    def test_the_secret_is_stored_under_a_connector_scoped_name_and_masked(self):
        installed = self.install()
        self.assertNotIn("SETU-SECRET-VALUE", json.dumps(installed))
        live = self.storage.get_connector("setu_pan", include_secrets=True)
        self.assertEqual(live["config"]["setu_pan_client_secret"], "SETU-SECRET-VALUE")


# ── FinBox BankConnect ──────────────────────────────────────────────────────────────────────
def _finbox(status="completed", accounts=None):
    return {"entity_id": "ent-1",
            "progress": [{"status": status, "message": None, "statement_id": "st-1", "source": "pdf"}],
            "predictors": accounts if accounts is not None else [
                {"account_id": "acc-1", "predictors": {"avg_monthly_closing_balance": 27637.93,
                                                       "total_salary_0": 62000, "total_emi_ecs_loan": 8500,
                                                       "inward_chq_bounces": 1}},
                {"account_id": "acc-2", "predictors": {"avg_monthly_closing_balance": 1000.07,
                                                       "total_salary_0": 0, "total_emi_ecs_loan": 0,
                                                       "inward_chq_bounces": 0}}]}


class FinBoxTemplateTest(_TemplateTest):
    TEMPLATE = "finbox_bankconnect_predictors"
    INSTALL = {"secrets": {"api_key": "FINBOX-KEY", "server_hash_secret": "FINBOX-HASH"}}
    reply: Tuple[int, Any] = (200, None)

    def handler(self, method, path, headers, body):
        status, payload = self.reply
        return status, payload if payload is not None else _finbox()

    def test_completed_predictors_become_summed_variables(self):
        self.install()
        ctx = self.decide({"finbox_entity_id": "ent-1"})
        self.assertEqual(ctx.outcome, "approve")
        self.assertEqual(ctx.variables["finbox_bank_avg_monthly_closing_balance"], 28638.0)
        self.assertEqual(ctx.variables["finbox_bank_salary_last_month"], 62000.0)
        self.assertEqual(ctx.variables["finbox_bank_inward_cheque_bounces"], 1.0)
        sent = self.provider.requests[0]
        self.assertEqual(sent["path"], "/bank-connect/v1/entity/ent-1/predictors/")
        self.assertEqual(sent["headers"]["x-api-key"], "FINBOX-KEY")
        self.assertEqual(sent["headers"]["server-hash"], "FINBOX-HASH")

    def test_a_statement_still_processing_is_not_data(self):
        self.reply = (200, _finbox(status="processing", accounts=[]))
        self.install()
        ctx = self.decide({"finbox_entity_id": "ent-1"})
        self.assertEqual(self.fetch_entry(ctx)["outcome"], "finbox_statement_processing")
        self.assertEqual(ctx.outcome, "review")

    def test_one_processing_statement_among_completed_ones_still_fails(self):
        body = _finbox()
        body["progress"].append({"status": "processing", "message": None, "statement_id": "st-2", "source": "pdf"})
        self.reply = (200, body)
        self.install()
        ctx = self.decide({"finbox_entity_id": "ent-1"})
        self.assertEqual(self.fetch_entry(ctx)["outcome"], "finbox_statement_processing",
                         "a partial result read as complete would under-count balances and bounces")

    def test_a_failed_statement_is_named(self):
        self.reply = (200, _finbox(status="failed"))
        self.install()
        self.assertEqual(self.fetch_entry(self.decide({"finbox_entity_id": "ent-1"}))["outcome"],
                         "finbox_statement_failed")

    def test_no_accounts_is_not_zero_balance(self):
        self.reply = (200, _finbox(accounts=[]))
        self.install()
        self.assertEqual(self.fetch_entry(self.decide({"finbox_entity_id": "ent-1"}))["outcome"],
                         "finbox_no_accounts")

    def test_the_documented_401_is_named_and_not_retried(self):
        self.reply = (401, {"detail": "Authentication credentials were not provided."})
        self.install()
        entry = self.fetch_entry(self.decide({"finbox_entity_id": "ent-1"}))
        self.assertEqual(entry["outcome"], "finbox_unauthorized")
        self.assertIn("Authentication credentials were not provided.", entry["error"])
        self.assertEqual(len(self.provider.requests), 1, "a 401 was retried; the template allows 1 retry")

    def test_a_503_is_retried(self):
        """The control for the test above: retries still happen where they can help."""
        self.reply = (503, {"detail": "down"})
        self.install()
        self.decide({"finbox_entity_id": "ent-1"})
        self.assertEqual(len(self.provider.requests), 2)

    def test_the_entity_id_cannot_walk_the_path(self):
        self.install()
        entry = self.fetch_entry(self.decide({"finbox_entity_id": "../../../admin"}))
        self.assertEqual(entry["outcome"], "invalid_input")
        self.assertEqual(self.provider.requests, [])

    def test_a_predictor_no_account_reports_sends_the_decision_to_review(self):
        self.reply = (200, _finbox(accounts=[{"account_id": "a", "predictors": {"total_salary_0": 5}}]))
        self.install()
        ctx = self.decide({"finbox_entity_id": "ent-1"})
        self.assertNotIn(ctx.variables.get("finbox_bank_avg_monthly_closing_balance"), (0, 0.0),
                         "an absent balance read as zero")
        self.assertEqual(ctx.outcome, "review")


# ── Equifax consumer credit report ─────────────────────────────────────────────────────────
# Trimmed from the CreditReportResponse example in Equifax's spec. Note the model ORDER: an
# unrequested FICO Bankcard model first, the requested 02583 second with score 0.
def _equifax(hit="1", requested_score: Any = 0, status="completed"):
    return {"status": status, "consumers": {"equifaxUSConsumerCreditReport": [{
        "identifier": "Individual Report 1", "hitCode": {"code": hit, "description": "Hit"},
        "fileSinceDate": "09012002",
        "models": [{"type": "00409", "score": "409", "reasons": [{"code": "00038"}]},
                   {"type": "MODEL", "modelNumber": "02583", "score": requested_score,
                    "modelIDOrScorecard": "1"}],
        "trades": [{"customerName": "HOUSTON FINANCE", "balance": 213, "pastDueAmount": 54}],
        "inquiries": []}]}}


APPLICANT = {"first_name": "SHEMICA", "last_name": "XXSHJF", "ssn": "666795307", "date_of_birth": "04281987",
             "house_number": "7249", "street_name": "PAJJ ZAFW", "street_type": "LN", "city": "HUMBLE",
             "state": "TX", "zip": "77396"}


class EquifaxTemplateTest(_TemplateTest):
    TEMPLATE = "equifax_us_consumer_credit_report"
    INSTALL = {"environment": "sandbox",
               "settings": {"token_url": "https://api.sandbox.equifax.com/v2/oauth/token",
                            "client_id": "efx-client", "member_number": "999XX00000",
                            "model_id": "02583", "end_user_name": "ALABASTER INC"},
               "secrets": {"client_secret": "EFX-CLIENT-SECRET", "security_code_secret": "EFX-SECURITY"}}
    report: Tuple[int, Any] = (200, None)

    def handler(self, method, path, headers, body):
        if path.endswith("/oauth/token"):
            return 200, {"access_token": "efx-token-1", "token_type": "Bearer", "expires_in": 3600}
        status, payload = self.report
        return status, payload if payload is not None else _equifax(requested_score=712)

    def test_the_documented_flow_token_then_report(self):
        self.install()
        ctx = self.decide(APPLICANT)
        self.assertEqual(ctx.outcome, "approve", ctx.source_fetches)
        token = self.provider.to("/v2/oauth/token")[0]
        expected = "Basic " + base64.b64encode(b"efx-client:EFX-CLIENT-SECRET").decode()
        self.assertEqual(token["headers"]["authorization"], expected)
        form = parse_qs(token["body"])
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["scope"], ["https://api.equifax.com/business/consumer-credit/v1"])
        report = self.provider.to("/business/consumer-credit/v1/reports/credit-report")[0]
        self.assertEqual(report["headers"]["authorization"], "Bearer efx-token-1")
        sent = json.loads(report["body"])
        config = sent["customerConfiguration"]["equifaxUSConsumerCreditReport"]
        self.assertEqual(config["memberNumber"], "999XX00000")
        self.assertEqual(config["securityCode"], "EFX-SECURITY")
        self.assertEqual(config["models"], [{"identifier": "02583"}])
        self.assertEqual(sent["consumers"]["socialNum"], [{"identifier": "current", "number": "666795307"}])
        self.assertEqual(sent["customerReferenceIdentifier"], ctx.execution_id)
        self.assertNotIn("EFX-CLIENT-SECRET", json.dumps(report), "the client secret reached the report endpoint")

    def test_the_score_is_the_requested_model_not_the_first(self):
        self.install()
        ctx = self.decide(APPLICANT)
        self.assertEqual(ctx.variables["equifax_ccr_score"], 712, "read models[0] (the FICO 409) instead")

    def test_equifaxs_own_example_score_of_zero_goes_to_review(self):
        self.report = (200, _equifax(requested_score=0))
        self.install()
        ctx = self.decide(APPLICANT)
        self.assertNotEqual(ctx.variables.get("equifax_ccr_score"), 0)
        self.assertEqual(ctx.outcome, "review")

    def test_hit_codes_map_to_named_failures(self):
        self.install()
        for code, expected in (("2", "equifax_no_hit"), ("A", "equifax_file_frozen"),
                               ("I", "equifax_inquiry_flagged_fraud"), ("7", "equifax_unexpected_hit_code")):
            self.report = (200, _equifax(hit=code, requested_score=712))
            connector_fetch.reset_cache()
            entry = self.fetch_entry(self.decide(APPLICANT))
            self.assertEqual(entry["outcome"], expected, code)

    def test_a_completed_status_carrying_a_validation_error_is_not_completed(self):
        self.report = (200, _equifax(status="completed === Request Schema Validation Error", requested_score=712))
        self.install()
        self.assertEqual(self.fetch_entry(self.decide(APPLICANT))["outcome"], "equifax_transaction_not_completed")

    def test_an_efx_error_body_is_named_with_its_code(self):
        self.report = (400, {"efxErrorCode": "400.01", "description": "Invalid JSON payload"})
        self.install()
        entry = self.fetch_entry(self.decide(APPLICANT))
        self.assertEqual(entry["outcome"], "equifax_bad_request")
        self.assertIn("400.01 / Invalid JSON payload", entry["error"])

    def test_a_policy_that_never_reads_the_bureau_does_not_pull_it(self):
        """Compute-everything mode (the default) computed every variable, so every opted-in source
        any variable read was fetched: installing this template made EVERY decision in the
        workspace pull a credit report -- billed, and an inquiry without a permissible purpose
        for that application. And skipping the fetch alone sent every decision to review,
        because the score variable fails closed on an absent report."""
        self.install()
        policy = {"id": "no_bureau", "name": "No bureau", "status": "prod", "defaultOutcome": "review",
                  "steps": [{"id": "s_out", "type": "outcome", "ref_id": "approve"}]}
        ctx = asyncio.run(PolicyExecutor(self.storage).execute(policy, {"custom": APPLICANT}, self.tenant_id,
                                                               source=LIVE_SOURCE))
        self.assertEqual(self.provider.requests, [], "a policy with no bureau step pulled a credit report")
        self.assertEqual(ctx.outcome, "approve", "the unreached bureau's variables sent it to review")
        self.assertNotIn("equifax_ccr_score", ctx.variables)

    def test_a_rule_that_reads_the_score_does_pull_it(self):
        """The control: reachability through a rule, with no connector step, still fetches."""
        self.install()
        self.storage.create_rule(
            {"id": "efx_gate", "name": "score gate", "status": "prod", "outcome": "approve",
             "nodes": [{"id": "n1", "type": "condition", "variable": "equifax_ccr_score",
                        "operator": ">", "value": "700"},
                       {"id": "n2", "type": "approve"}]}, tenant_id=self.tenant_id)
        fast_decide.invalidate(self.tenant_id)
        policy = {"id": "bureau_gate", "name": "Bureau gate", "status": "prod", "defaultOutcome": "review",
                  "steps": [{"id": "s1", "type": "rule", "ref_id": "efx_gate"},
                            {"id": "s2", "type": "outcome", "ref_id": "approve"}]}
        ctx = asyncio.run(PolicyExecutor(self.storage).execute(policy, {"custom": APPLICANT}, self.tenant_id,
                                                               source=LIVE_SOURCE))
        self.assertEqual(len(self.provider.to("/business/consumer-credit/v1/reports/credit-report")), 1)
        self.assertEqual(ctx.variables["equifax_ccr_score"], 712)

    def test_a_missing_ssn_spends_neither_a_token_nor_a_pull(self):
        self.install()
        applicant = {k: v for k, v in APPLICANT.items() if k != "ssn"}
        entry = self.fetch_entry(self.decide(applicant))
        self.assertEqual(entry["outcome"], "missing_input")
        self.assertEqual(self.provider.requests, [])


# ── the catalogue and install surface ──────────────────────────────────────────────────────
class TemplateCatalogueTest(_TemplateTest):
    TEMPLATE = "setu_pan_verification"
    INSTALL = SetuPanTemplateTest.INSTALL

    def handler(self, method, path, headers, body):
        return 200, SETU_VALID

    def test_three_named_providers_one_per_category(self):
        rows = self.client.get("/api/v1/connector-templates", headers=self.key).json()
        self.assertEqual({r["category"] for r in rows}, {"bureau", "bank_statement", "kyc"})
        self.assertEqual({r["provider"] for r in rows}, {"Equifax", "FinBox", "Setu"})

    def test_every_template_says_what_was_not_verified(self):
        for template_id in connector_templates.TEMPLATES:
            detail = self.client.get("/api/v1/connector-templates/" + template_id, headers=self.key).json()
            self.assertTrue(detail["verification"]["verified"], template_id)
            self.assertTrue(detail["verification"]["not_verified"], template_id)

    def test_every_problem_is_reported_at_once(self):
        response = self.client.post("/api/v1/connector-templates/equifax_us_consumer_credit_report/install",
                                    json={"settings": {"model_id": "x" * 40}}, headers=self.key)
        self.assertEqual(response.status_code, 422)
        problems = response.json()["detail"]["problems"]
        for needle in ("token_url", "client_id", "member_number", "model_id", "client_secret",
                       "security_code_secret"):
            self.assertTrue(any(needle in p for p in problems), (needle, problems))

    def test_an_environment_without_a_published_url_requires_one(self):
        response = self.client.post("/api/v1/connector-templates/equifax_us_consumer_credit_report/install",
                                    json={**EquifaxTemplateTest.INSTALL, "environment": "production"},
                                    headers=self.key)
        self.assertEqual(response.status_code, 422)
        self.assertIn("base_url", json.dumps(response.json()))

    def test_reinstalling_is_a_conflict_not_a_second_connector(self):
        self.install()
        again = self.client.post("/api/v1/connector-templates/setu_pan_verification/install",
                                 json=self.INSTALL, headers=self.key)
        self.assertEqual(again.status_code, 409)
        self.assertIn("setu_pan", again.json()["detail"]["ids"])

    def test_two_installs_keep_their_secrets_apart(self):
        """The {{secrets.*}} namespace is flattened across the tenant, so a fixed key name would
        let the second install's secret silently replace the first's."""
        self.install()
        self.install(connector_id="setu_pan_prod", environment="production",
                     secrets={"client_secret": "PROD-SECRET"})
        self.decide({"pan": "ABCDE1234A", "pan_consent": "Y"}, connector_id="setu_pan")
        self.decide({"pan": "ABCDE1234A", "pan_consent": "Y"}, connector_id="setu_pan_prod")
        sent = [r["headers"]["x-client-secret"] for r in self.provider.requests]
        self.assertEqual(sent, ["SETU-SECRET-VALUE", "PROD-SECRET"])

    def test_an_install_belongs_to_the_installing_tenant_only(self):
        self.install()
        other = self.storage.create_tenant("Other Co")["id"]
        other_key = {"x-api-key": self.storage.generate_api_key_for_tenant(other)["plaintext"]}
        mine = self.client.post("/api/v1/connector-templates/setu_pan_verification/install",
                                json=self.INSTALL, headers=other_key)
        self.assertEqual(mine.status_code, 200, "another tenant's install of the same id conflicted")
        self.assertIsNone(self.storage.get_connector("setu_pan", tenant_id=self.storage.create_tenant("Third")["id"]))
        listed = self.client.get("/api/v1/connectors", headers=other_key).json()
        self.assertEqual([c["id"] for c in listed if c["id"] == "setu_pan"], ["setu_pan"],
                         "the other tenant sees exactly its own install")
        self.assertEqual(self.storage.get_connector("setu_pan", include_secrets=True, tenant_id=other)
                         ["config"]["setu_pan_client_secret"], "SETU-SECRET-VALUE")

    def test_a_viewer_cannot_install(self):
        viewer = {"x-api-key": self.storage.generate_api_key_for_tenant(self.tenant_id, role="viewer")["plaintext"]}
        response = self.client.post("/api/v1/connector-templates/setu_pan_verification/install",
                                    json=self.INSTALL, headers=viewer)
        self.assertEqual(response.status_code, 403)

    def test_the_contract_page_is_generated_from_the_templates(self):
        import subprocess

        script = APP_ROOT.parents[1] / "scripts" / "gen_connector_template_docs.py"
        result = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_documented_inputs_are_the_enforced_inputs(self):
        """`inputs` is what the docs and the install response tell an integrator to send;
        `required_inputs` is what the fetch enforces. Two lists that can disagree will."""
        for template in connector_templates.TEMPLATES.values():
            documented = {(i["path"], i.get("pattern")) for i in template["inputs"]}
            enforced = {(i["path"], i.get("pattern")) for i in template["connector"]["config"]["required_inputs"]}
            self.assertEqual(documented, enforced, template["id"])

    def test_starter_variables_are_installed_in_dev(self):
        installed = self.install()
        self.assertEqual({v["status"] for v in installed["variables"]}, {"dev"})


if __name__ == "__main__":
    unittest.main()
