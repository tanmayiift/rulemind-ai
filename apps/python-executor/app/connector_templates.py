"""Named-provider connector templates (plan 6.1).

THE GAP. The nine seeded connectors are SHAPES, not integrations: `bureau` is described as
"CIBIL / Experian" and points at `https://bureau.example.com/pull`. There was no prebuilt
integration with any real provider, so every evaluation ended at the integration question with
"you configure a generic HTTP client yourself".

WHAT A TEMPLATE IS. A connector configuration for one named provider's API that uses only the
engine's existing decision-time fetch contract (`connector_fetch`), plus starter variables:

  * the request envelope, built from the decision payload (`request_mapping`), with the inputs it
    needs declared and validated up front (`required_inputs`) -- so a missing SSN is
    `missing_input` naming the field, not an opaque 400 from the provider;
  * the response mapping into the source's fields (`response_mapping`);
  * the provider's own "200 that is not an answer" cases (still processing, no hit, file frozen)
    as named failures (`response_checks`) rather than a payload of Nones fed to a gate;
  * the provider's DOCUMENTED error statuses as stable codes (`error_codes`), with the provider's
    own error text carried into the trace (`error_detail_path`).

Failures route to review by default (`on_fetch_failure: review_gate`) -- the engine's standing
rule that a decision made without a source it declared it needs is not a conservative decision.

WHAT WAS VERIFIED, AND WHAT WAS NOT. Every template records its sources and says plainly which
parts were read from the provider's published contract and which were not. None has been run
against the provider's live sandbox, because that needs credentials the provider issues to a
customer. The tests in `tests/test_connector_templates.py` run each template end to end against a
local server that serves the provider's OWN documented example bodies -- evidence that the
template matches the published contract, not that the provider still honours it.

INSTALL-TIME PARAMETERS use `<<name>>`; decision-time placeholders keep the engine's
`{{payload.*}}` / `{{secrets.*}}` syntax. The two never mix: `<<...>>` is rendered once when the
template is installed, so a setting like the Equifax member number is baked into the connector,
while the applicant's SSN is resolved per decision.

SECRET KEY NAMES are derived from the connector id (`<<connector_id>>_api_key`). The
`{{secrets.*}}` namespace is flattened across every connector in the tenant, so a fixed name like
`api_key` would collide the moment a template is installed twice (sandbox and production) or next
to another connector -- and the later one would silently win.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

VERIFIED_ON = "2026-09-24"

# ── Setu — PAN verification (KYC, India) ───────────────────────────────────────────────────
_SETU_PAN: Dict[str, Any] = {
    "id": "setu_pan_verification",
    "provider": "Setu",
    "product": "PAN verification",
    "category": "kyc",
    "region": "IN",
    "description": "Verify a customer's PAN against NSDL through Setu, with the name on record "
                   "and Aadhaar seeding status.",
    "default_connector_id": "setu_pan",
    "sources": {
        "docs": "https://docs.setu.co/data/pan/quickstart",
        "spec": "https://github.com/SetuHQ/docs/blob/main/api-references/data/pan.json",
    },
    "verification": {
        "checked_on": VERIFIED_ON,
        "verified": [
            "Base URLs, endpoint, method and the three auth headers -- Setu's own OpenAPI 3.0 spec.",
            "Request fields pan / consent / reason, and that consent must be Y or y.",
            "Every response body the docs publish: valid PAN (200), invalid PAN (200), consent "
            "missing and reason too short (400), PAN not found (404), internal error (500).",
            "Sandbox test values: ABCDE1234A is valid, ABCDE1234B is invalid, anything else 404s.",
        ],
        "not_verified": [
            "Not run against Setu's sandbox: x-client-id, x-client-secret and "
            "x-product-instance-id are issued by Setu on request.",
            "No 401 body is documented, so an auth failure surfaces as a generic auth_failed.",
        ],
        "discrepancies": [
            "The quickstart shows verification \"success\" / \"failed\" in lower case; the OpenAPI "
            "example shows \"SUCCESS\". The check accepts both cases rather than betting on one.",
        ],
    },
    "environments": {"sandbox": "https://dg-sandbox.setu.co", "production": "https://dg.setu.co"},
    "settings": [
        {"key": "client_id", "label": "x-client-id", "required": True,
         "description": "Issued by Setu with your credentials."},
        {"key": "product_instance_id", "label": "x-product-instance-id", "required": True,
         "description": "Issued by Setu per product instance."},
        {"key": "reason", "label": "Reason sent with every verification", "required": True,
         "default": "Identity verification for a credit application",
         "pattern": r"^.{20,}$",
         "description": "Setu rejects a reason shorter than 20 characters (insufficient_reason_length)."},
    ],
    "secrets": [
        {"key": "client_secret", "label": "x-client-secret", "description": "Issued by Setu."},
    ],
    "inputs": [
        {"path": "payload.custom.pan", "label": "PAN", "pattern": r"^[A-Za-z0-9]{10}$",
         "description": "The customer's 10-character PAN."},
        {"path": "payload.custom.pan_consent", "label": "Consent", "pattern": r"^[YyNn]$",
         "description": "Y when the customer consented to PAN verification. Supplied per "
                        "application by the caller -- never hardcoded, because asserting consent "
                        "that was not collected is the lender's liability, not a default."},
    ],
    "connector": {
        "name": "Setu PAN verification",
        "icon": "kyc",
        "color": "#0f766e",
        "description": "Setu PAN verification (NSDL). Template setu_pan_verification.",
        "schema_paths": ["verification", "message", "full_name", "category",
                         "aadhaar_seeding_status", "trace_id"],
        "sample_payload": {
            # Setu's documented success example, passed through this template's response_mapping.
            "verification": "SUCCESS", "message": "PAN is valid", "full_name": "John Doe",
            "category": "Individual", "aadhaar_seeding_status": "LINKED",
            "trace_id": "1-6346a91a-620cf6cc4f68d2e30316881e",
        },
        "config": {
            "auth_type": "none",
            "base_url": "<<base_url>>",
            "fetch_at_decision": True,
            "on_fetch_failure": "review_gate",
            "timeout_ms": 5000,
            "retries": 0,
            "headers": {
                "x-client-id": "<<client_id>>",
                "x-client-secret": "{{secrets.<<connector_id>>_client_secret}}",
                "x-product-instance-id": "<<product_instance_id>>",
            },
            "required_inputs": [
                {"path": "payload.custom.pan", "pattern": r"^[A-Za-z0-9]{10}$"},
                {"path": "payload.custom.pan_consent", "pattern": r"^[YyNn]$"},
            ],
            "request_mapping": {
                "method": "POST",
                "path": "/api/verify/pan",
                "body": {"pan": "{{payload.custom.pan}}", "consent": "{{payload.custom.pan_consent}}",
                         "reason": "<<reason>>"},
            },
            # An invalid PAN is an ANSWER (200, verification "failed") and stays data: the policy
            # decides what an invalid PAN means. Anything outside the documented values is not.
            "response_checks": [
                {"path": "$.verification", "in": ["success", "SUCCESS", "failed", "FAILED"],
                 "code": "setu_unexpected_verification_value"},
            ],
            "response_mapping": {
                "verification": "$.verification",
                "message": "$.message",
                "full_name": "$.data.full_name",
                "category": "$.data.category",
                "aadhaar_seeding_status": "$.data.aadhaar_seeding_status",
                "trace_id": "$.traceId",
            },
            "error_codes": {"400": "setu_bad_request", "404": "setu_pan_not_found",
                            "500": "setu_internal_error"},
            "error_detail_path": ["$.error.code", "$.message"],
        },
    },
    "variables": [
        {"id": "pan_verified", "name": "PAN verified", "category": "KYC",
         "description": "True when Setu reports the PAN valid. An invalid PAN is False, not missing.",
         "code": ("@variable(source=\"<<connector_id>>\")\n"
                  "def <<connector_id>>_pan_verified(payload, variables, apis):\n"
                  "    return str(payload.get(\"verification\") or \"\").lower() == \"success\"\n")},
        {"id": "aadhaar_linked", "name": "PAN linked to Aadhaar", "category": "KYC",
         "description": "True/False from aadhaar_seeding_status. Setu marks the field optional; when "
                        "it is absent this raises, so the decision goes to review instead of "
                        "treating 'not reported' as 'not linked'.",
         "code": ("@variable(source=\"<<connector_id>>\")\n"
                  "def <<connector_id>>_aadhaar_linked(payload, variables, apis):\n"
                  "    status = payload.get(\"aadhaar_seeding_status\")\n"
                  "    if not status:\n"
                  "        raise ValueError(\"Setu did not report aadhaar_seeding_status\")\n"
                  "    return str(status).upper() == \"LINKED\"\n")},
    ],
}

# ── FinBox — BankConnect predictors (bank statements, India) ────────────────────────────────
_FINBOX: Dict[str, Any] = {
    "id": "finbox_bankconnect_predictors",
    "provider": "FinBox",
    "product": "BankConnect predictors",
    "category": "bank_statement",
    "region": "IN",
    "description": "Bank-statement predictors for an entity whose statements were uploaded to "
                   "FinBox BankConnect: balances, salary, EMI outflow and cheque bounces.",
    "default_connector_id": "finbox_bank",
    "sources": {"docs": "https://docs.finbox.in/bank-connect/rest-api.html"},
    "verification": {
        "checked_on": VERIFIED_ON,
        "verified": [
            "Base URL https://apis.bankconnect.finbox.in/bank-connect/v1/entity/<entity_id>/ and "
            "the x-api-key + server-hash headers.",
            "GET /predictors/ response: entity_id, progress (an ARRAY of {status, message, "
            "statement_id, source}), predictors (an array of {account_id, predictors}).",
            "Predictor keys avg_monthly_closing_balance, total_salary_0, total_emi_ecs_loan, "
            "inward_chq_bounces, outward_chq_bounces.",
            "progress.status is one of processing / completed / failed.",
            "401 body {\"detail\": \"Authentication credentials were not provided.\"}.",
        ],
        "not_verified": [
            "Not run against FinBox: API keys are issued by FinBox.",
            "The docs publish no sandbox host, so the template's only environment is production.",
            "Only 200 and 401 are documented. Other statuses fall back to the engine's generic "
            "codes rather than to names this template would have had to invent.",
            "Statements must already be uploaded (FinBox's upload or Account Aggregator flow); "
            "this template only reads the result, keyed by the entity id the caller supplies.",
        ],
    },
    "environments": {"production": "https://apis.bankconnect.finbox.in"},
    "settings": [],
    "secrets": [
        {"key": "api_key", "label": "x-api-key", "description": "Issued by FinBox."},
        {"key": "server_hash_secret", "label": "server-hash", "description": "Issued by FinBox."},
    ],
    "inputs": [
        {"path": "payload.custom.finbox_entity_id", "label": "BankConnect entity id",
         "pattern": r"^[A-Za-z0-9_-]{1,64}$",
         "description": "The entity the customer's statements were uploaded under. Pattern-checked "
                        "because it is placed in the URL path."},
    ],
    "connector": {
        "name": "FinBox BankConnect",
        "icon": "bank",
        "color": "#1d4ed8",
        "description": "FinBox BankConnect predictors. Template finbox_bankconnect_predictors.",
        "schema_paths": ["progress", "accounts"],
        "sample_payload": {
            # FinBox's documented /predictors/ example, passed through this template's mapping.
            "progress": [{"status": "completed", "message": None,
                          "statement_id": "uuid4_for_statement", "source": "pdf"}],
            "accounts": [{"account_id": "uuid4_for_account",
                          "predictors": {"end_date": "15-Apr-20", "start_date": "29-Sep-19",
                                         "avg_expense_to_income_perc": 114.0,
                                         "avg_monthly_closing_balance": 27637.93}}],
        },
        "config": {
            "auth_type": "none",
            "base_url": "<<base_url>>",
            "fetch_at_decision": True,
            "on_fetch_failure": "review_gate",
            "timeout_ms": 5000,
            "retries": 1,
            "headers": {
                "x-api-key": "{{secrets.<<connector_id>>_api_key}}",
                "server-hash": "{{secrets.<<connector_id>>_server_hash_secret}}",
            },
            "required_inputs": [
                {"path": "payload.custom.finbox_entity_id", "pattern": r"^[A-Za-z0-9_-]{1,64}$"},
            ],
            "request_mapping": {
                "method": "GET",
                "path": "/bank-connect/v1/entity/{{payload.custom.finbox_entity_id}}/predictors/",
            },
            # A 200 while FinBox is still processing a statement is NOT data: its predictors are
            # absent or partial. Every statement must be complete, and there must be an account.
            "response_checks": [
                {"path": "$.progress", "each": "status", "in": ["completed"],
                 "code": "finbox_statement_not_completed",
                 "codes": {"processing": "finbox_statement_processing",
                           "failed": "finbox_statement_failed"}},
                {"path": "$.predictors", "min_items": 1, "code": "finbox_no_accounts"},
            ],
            "response_mapping": {"progress": "$.progress", "accounts": "$.predictors"},
            "error_codes": {"401": "finbox_unauthorized"},
            "error_detail_path": "$.detail",
        },
    },
    "variables": [],  # filled below by _finbox_sum_variable
}


def _finbox_sum_variable(key: str, var_id: str, name: str, description: str) -> Dict[str, Any]:
    """A predictor summed across every account in the entity. Raises when NO account reports it,
    so an absent predictor sends the decision to review rather than reading as zero -- zero
    bounces and "FinBox did not say" are different answers."""
    return {
        "id": var_id, "name": name, "category": "Banking", "description": description,
        "code": ("@variable(source=\"<<connector_id>>\")\n"
                 "def <<connector_id>>_" + var_id + "(payload, variables, apis):\n"
                 "    values = [(account.get(\"predictors\") or {}).get(\"" + key + "\")\n"
                 "              for account in payload.get(\"accounts\") or []]\n"
                 "    present = [float(value) for value in values if value is not None]\n"
                 "    if not present:\n"
                 "        raise ValueError(\"FinBox reported no " + key + " for any account\")\n"
                 "    return round(sum(present), 2)\n"),
    }


_FINBOX["variables"] = [
    _finbox_sum_variable("avg_monthly_closing_balance", "avg_monthly_closing_balance",
                         "Avg monthly closing balance", "Summed across every account in the entity."),
    _finbox_sum_variable("total_salary_0", "salary_last_month", "Salary credited, latest month",
                         "FinBox total_salary_0 (month_0 is the latest), summed across accounts."),
    _finbox_sum_variable("total_emi_ecs_loan", "emi_outflow", "EMI / ECS loan outflow",
                         "FinBox total_emi_ecs_loan, summed across accounts."),
    _finbox_sum_variable("inward_chq_bounces", "inward_cheque_bounces", "Inward cheque bounces",
                         "FinBox inward_chq_bounces, summed across accounts."),
]

# ── Equifax — US Consumer Credit Report (bureau) ────────────────────────────────────────────
_EFX_REPORT = "$.consumers.equifaxUSConsumerCreditReport[0]"

_EQUIFAX: Dict[str, Any] = {
    "id": "equifax_us_consumer_credit_report",
    "provider": "Equifax",
    "product": "US Consumer Credit Report (JSON API)",
    "category": "bureau",
    "region": "US",
    "description": "Pull an Equifax consumer credit report: hit status, the score for the model "
                   "you are contracted for, trades and inquiries. OAuth2 client credentials.",
    "default_connector_id": "equifax_ccr",
    "sources": {
        "guide": "https://assets.equifax.com/acro-migration-kb/assets/consumer_credit_json_api_getting_started_guide.pdf",
        "spec": "https://github.com/fullstack-development/haskell-equifax-generated/blob/master/openapi.yaml",
        "portal": "https://developer.equifax.com/products/apiproducts/oneview-consumer-credit-report",
    },
    "verification": {
        "checked_on": VERIFIED_ON,
        "verified": [
            "OAuth2 client credentials, client id and secret sent as HTTP Basic, token used as "
            "Bearer -- Equifax's own quick start guide.",
            "Sandbox base URL, POST /reports/credit-report, and the scope "
            "https://api.equifax.com/business/consumer-credit/v1 -- Equifax's portal OpenAPI "
            "spec, as republished in a public repository (the portal itself requires a "
            "corporate-email login).",
            "Request envelope (consumers.name / socialNum / dateOfBirth / addresses, "
            "customerConfiguration.equifaxUSConsumerCreditReport.memberNumber / securityCode / "
            "models / endUserInformation) -- that spec's published example.",
            "Response: status, consumers.equifaxUSConsumerCreditReport[].hitCode, models[], "
            "trades[], inquiries[]; the full hitCode taxonomy; efxErrorCode error bodies for "
            "400/401/403/404/415/500/501/503/504.",
        ],
        "not_verified": [
            "Not run against Equifax's sandbox: client credentials are issued to a registered "
            "company.",
            "The token endpoint URL and the production/UAT base URLs are NOT in the public "
            "material, so the template takes them as install settings rather than guessing.",
            "The spec is Equifax's, but obtained from a third-party repository that generated a "
            "client from it; confirm against the portal's API Reference when you register.",
        ],
        "discrepancies": [
            "In Equifax's own example the FIRST model is an unrequested FICO Bankcard score and "
            "the REQUESTED model (02583) comes second, with score 0. A path like models[0].score "
            "reads the wrong model, so the template maps the whole models array and the starter "
            "variable selects by model number.",
            "status is documented as possibly \"completed === Request Schema Validation Error\": a "
            "200 carrying a validation failure. The check requires exactly \"completed\".",
            "score is a string (\"409\") in one model and an integer (0) in another.",
        ],
    },
    "environments": {"sandbox": "https://api.sandbox.equifax.com/business/consumer-credit/v1"},
    "settings": [
        {"key": "token_url", "label": "Token endpoint", "required": True, "pattern": r"^https://",
         "description": "From your app's credentials page in the Equifax developer portal."},
        {"key": "client_id", "label": "Client ID", "required": True,
         "description": "Equifax issues one per app and environment."},
        {"key": "member_number", "label": "Member number", "required": True,
         "pattern": r"^[0-9A-Za-z]{10}$",
         "description": "Your Equifax member number (test member numbers start with 999)."},
        {"key": "model_id", "label": "Score model number", "required": True,
         "pattern": r"^[0-9A-Za-z]{1,10}$",
         "description": "The model your contract returns, e.g. 02583 in Equifax's example."},
        {"key": "end_user_name", "label": "End user name", "required": True,
         "description": "endUserInformation.endUsersName -- the business the report is for."},
        {"key": "permissible_purpose_code", "label": "Permissible purpose code", "required": True,
         "default": "01", "pattern": r"^[0-9A-Za-z]{2}$",
         "description": "FCRA permissible purpose, as agreed with Equifax."},
    ],
    "secrets": [
        {"key": "client_secret", "label": "Client secret",
         "description": "Sent to the token endpoint only, never to the report endpoint."},
        {"key": "security_code_secret", "label": "Security code",
         "description": "The member number's security code, sent in the request body."},
    ],
    "inputs": [
        {"path": "payload.custom.first_name", "label": "First name"},
        {"path": "payload.custom.last_name", "label": "Last name"},
        {"path": "payload.custom.ssn", "label": "SSN", "pattern": r"^[0-9]{9}$"},
        {"path": "payload.custom.date_of_birth", "label": "Date of birth (MMDDYYYY)",
         "pattern": r"^[0-9]{8}$"},
        {"path": "payload.custom.house_number", "label": "House number"},
        {"path": "payload.custom.street_name", "label": "Street name"},
        {"path": "payload.custom.street_type", "label": "Street type (LN, ST, AVE...)"},
        {"path": "payload.custom.city", "label": "City"},
        {"path": "payload.custom.state", "label": "State", "pattern": r"^[A-Za-z]{2}$"},
        {"path": "payload.custom.zip", "label": "ZIP", "pattern": r"^[0-9]{5}$"},
    ],
    "connector": {
        "name": "Equifax consumer credit report",
        "icon": "bureau",
        "color": "#9b1c31",
        "description": "Equifax US Consumer Credit Report. Template equifax_us_consumer_credit_report.",
        "schema_paths": ["hit_code", "file_since_date", "models", "trades", "inquiries"],
        "sample_payload": {
            "hit_code": "1", "file_since_date": "09012002",
            "models": [{"type": "MODEL", "modelNumber": "02583", "score": 712}],
            "trades": [{"customerName": "HOUSTON FINANCE", "balance": 213, "pastDueAmount": 54}],
            "inquiries": [],
        },
        "config": {
            "auth_type": "oauth2",
            "token_url": "<<token_url>>",
            "client_id": "<<client_id>>",
            "token_auth_method": "client_secret_basic",
            "scope": "https://api.equifax.com/business/consumer-credit/v1",
            "base_url": "<<base_url>>",
            "fetch_at_decision": True,
            "on_fetch_failure": "review_gate",
            "timeout_ms": 8000,
            "retries": 0,
            "required_inputs": [
                {"path": "payload.custom.first_name"},
                {"path": "payload.custom.last_name"},
                {"path": "payload.custom.ssn", "pattern": r"^[0-9]{9}$"},
                {"path": "payload.custom.date_of_birth", "pattern": r"^[0-9]{8}$"},
                {"path": "payload.custom.house_number"},
                {"path": "payload.custom.street_name"},
                {"path": "payload.custom.street_type"},
                {"path": "payload.custom.city"},
                {"path": "payload.custom.state", "pattern": r"^[A-Za-z]{2}$"},
                {"path": "payload.custom.zip", "pattern": r"^[0-9]{5}$"},
            ],
            "request_mapping": {
                "method": "POST",
                "path": "/reports/credit-report",
                "body": {
                    "consumers": {
                        "name": [{"identifier": "current",
                                  "firstName": "{{payload.custom.first_name}}",
                                  "lastName": "{{payload.custom.last_name}}"}],
                        "socialNum": [{"identifier": "current", "number": "{{payload.custom.ssn}}"}],
                        "dateOfBirth": "{{payload.custom.date_of_birth}}",
                        "addresses": [{"identifier": "current",
                                       "houseNumber": "{{payload.custom.house_number}}",
                                       "streetName": "{{payload.custom.street_name}}",
                                       "streetType": "{{payload.custom.street_type}}",
                                       "city": "{{payload.custom.city}}",
                                       "state": "{{payload.custom.state}}",
                                       "zip": "{{payload.custom.zip}}"}],
                    },
                    # Ties Equifax's record of the pull to this decision.
                    "customerReferenceIdentifier": "{{execution_id}}",
                    "customerConfiguration": {
                        "equifaxUSConsumerCreditReport": {
                            "memberNumber": "<<member_number>>",
                            "securityCode": "{{secrets.<<connector_id>>_security_code_secret}}",
                            "codeDescriptionRequired": True,
                            "models": [{"identifier": "<<model_id>>"}],
                            "endUserInformation": {
                                "endUsersName": "<<end_user_name>>",
                                "permissiblePurposeCode": "<<permissible_purpose_code>>",
                            },
                            "ECOAInquiryType": "Individual",
                        },
                    },
                },
            },
            "response_checks": [
                {"path": "$.status", "in": ["completed"], "code": "equifax_transaction_not_completed"},
                # hitCode taxonomy from Equifax's spec. 1 = Hit, 6 = Hit + automated consumer
                # narrative; everything else is a report the decision cannot be made on.
                {"path": _EFX_REPORT + ".hitCode.code", "in": ["1", "6"],
                 "code": "equifax_unexpected_hit_code",
                 "codes": {
                     "2": "equifax_no_hit", "9": "equifax_no_hit", "C": "equifax_no_hit",
                     "3": "equifax_manual_file", "4": "equifax_manual_file",
                     "D": "equifax_manual_file", "E": "equifax_manual_file",
                     "5": "equifax_referred_file", "F": "equifax_referred_file",
                     "A": "equifax_file_frozen", "G": "equifax_file_frozen",
                     "L": "equifax_file_locked", "M": "equifax_file_locked",
                     "I": "equifax_inquiry_flagged_fraud", "J": "equifax_inquiry_flagged_fraud",
                 }},
            ],
            "response_mapping": {
                "hit_code": _EFX_REPORT + ".hitCode.code",
                "file_since_date": _EFX_REPORT + ".fileSinceDate",
                "models": _EFX_REPORT + ".models",
                "trades": _EFX_REPORT + ".trades",
                "inquiries": _EFX_REPORT + ".inquiries",
            },
            "error_codes": {
                "400": "equifax_bad_request", "401": "equifax_unauthorized",
                "403": "equifax_access_denied", "404": "equifax_not_found",
                "415": "equifax_unsupported_media_type", "500": "equifax_internal_error",
                "501": "equifax_not_implemented", "503": "equifax_unavailable",
                "504": "equifax_gateway_timeout",
            },
            "error_detail_path": ["$.efxErrorCode", "$.description"],
        },
    },
    "variables": [
        {"id": "score", "name": "Equifax score", "category": "Bureau",
         "description": "The score of the contracted model, selected by model number -- never by "
                        "position, because Equifax's own example puts an unrequested model first. "
                        "Raises when that model is absent or reports no positive score, so the "
                        "decision goes to review instead of deciding on 0.",
         "code": ("@variable(source=\"<<connector_id>>\")\n"
                  "def <<connector_id>>_score(payload, variables, apis):\n"
                  "    wanted = \"<<model_id>>\"\n"
                  "    for model in payload.get(\"models\") or []:\n"
                  "        if wanted in (str(model.get(\"modelNumber\") or \"\"), str(model.get(\"type\") or \"\")):\n"
                  "            try:\n"
                  "                score = int(str(model.get(\"score\")).strip())\n"
                  "            except (TypeError, ValueError):\n"
                  "                raise ValueError(\"Equifax model \" + wanted + \" has no numeric score\")\n"
                  "            if score <= 0:\n"
                  "                raise ValueError(\"Equifax model \" + wanted + \" returned no usable score\")\n"
                  "            return score\n"
                  "    raise ValueError(\"Equifax returned no score for model \" + wanted)\n")},
        {"id": "past_due_total", "name": "Past-due amount across trades", "category": "Bureau",
         "description": "Sum of pastDueAmount over every reported trade.",
         "code": ("@variable(source=\"<<connector_id>>\")\n"
                  "def <<connector_id>>_past_due_total(payload, variables, apis):\n"
                  "    return sum(float(trade.get(\"pastDueAmount\") or 0) for trade in payload.get(\"trades\") or [])\n")},
        {"id": "trade_count", "name": "Reported trades", "category": "Bureau",
         "description": "Number of trades on the file.",
         "code": ("@variable(source=\"<<connector_id>>\")\n"
                  "def <<connector_id>>_trade_count(payload, variables, apis):\n"
                  "    return len(payload.get(\"trades\") or [])\n")},
    ],
}

TEMPLATES: Dict[str, Dict[str, Any]] = {t["id"]: t for t in (_EQUIFAX, _FINBOX, _SETU_PAN)}


# ── catalogue ──────────────────────────────────────────────────────────────────────────────
_SUMMARY_KEYS = ("id", "provider", "product", "category", "region", "description",
                 "default_connector_id", "sources", "environments")


def list_templates(category: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = [t for t in TEMPLATES.values() if not category or t["category"] == category]
    return [{**{k: copy.deepcopy(t[k]) for k in _SUMMARY_KEYS},
             "verified_on": t["verification"]["checked_on"]} for t in rows]


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    template = TEMPLATES.get(template_id)
    return copy.deepcopy(template) if template else None


# ── install ────────────────────────────────────────────────────────────────────────────────
class TemplateInstallError(ValueError):
    """An install request the caller must fix. `problems` names every field at once, so a form
    can show them together instead of one per round trip."""

    def __init__(self, problems: List[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


_CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_PARAM = re.compile(r"<<([a-z_]+)>>")


def _render(value: Any, params: Dict[str, str]) -> Any:
    """Substitute `<<name>>` install parameters. An unknown name is a bug in the template, not in
    the request, so it raises rather than leaving `<<name>>` in a live connector."""
    if isinstance(value, str):
        def swap(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name not in params:
                raise KeyError("template references undefined parameter <<{0}>>".format(name))
            return params[name]
        return _PARAM.sub(swap, value)
    if isinstance(value, dict):
        return {_render(k, params): _render(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(item, params) for item in value]
    return value


def build_install(template_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """The connector and variables a template install would create. Pure -- storage is the
    caller's -- so the same function backs the install endpoint and the tests.

    request: {connector_id?, environment?, base_url?, settings: {}, secrets: {},
              install_variables?: bool}
    """
    template = TEMPLATES.get(template_id)
    if template is None:
        raise KeyError(template_id)
    problems: List[str] = []

    connector_id = str(request.get("connector_id") or template["default_connector_id"])
    if not _CONNECTOR_ID.match(connector_id):
        problems.append("connector_id must be 3-48 characters of a-z, 0-9 and _, starting with a letter")

    environments = template["environments"]
    environment = str(request.get("environment") or next(iter(environments)))
    base_url = request.get("base_url") or environments.get(environment)
    if not base_url:
        problems.append("environment {0!r} has no published base URL for {1}; supply base_url "
                        "(published: {2})".format(environment, template["provider"],
                                                  ", ".join(environments) or "none"))
    elif not str(base_url).startswith("https://"):
        problems.append("base_url must be https")

    supplied = request.get("settings") or {}
    settings: Dict[str, str] = {}
    for spec in template["settings"]:
        raw = supplied.get(spec["key"], spec.get("default"))
        value = "" if raw is None else str(raw).strip()
        if not value:
            if spec.get("required"):
                problems.append("setting {0!r} ({1}) is required".format(spec["key"], spec["label"]))
            continue
        if spec.get("pattern") and not re.match(spec["pattern"], value):
            problems.append("setting {0!r} does not match {1}".format(spec["key"], spec["pattern"]))
        settings[spec["key"]] = value
    unknown = sorted(set(supplied) - {s["key"] for s in template["settings"]})
    if unknown:
        problems.append("unknown settings: {0}".format(", ".join(unknown)))

    supplied_secrets = request.get("secrets") or {}
    secrets: Dict[str, str] = {}
    for spec in template["secrets"]:
        value = supplied_secrets.get(spec["key"])
        if not isinstance(value, str) or not value.strip():
            problems.append("secret {0!r} ({1}) is required".format(spec["key"], spec["label"]))
            continue
        secrets[spec["key"]] = value.strip()
    unknown = sorted(set(supplied_secrets) - {s["key"] for s in template["secrets"]})
    if unknown:
        problems.append("unknown secrets: {0}".format(", ".join(unknown)))

    if problems:
        raise TemplateInstallError(problems)

    params = {**settings, "connector_id": connector_id, "base_url": str(base_url).rstrip("/")}
    connector = _render(copy.deepcopy(template["connector"]), params)
    connector["id"] = connector_id
    config = connector["config"]
    for key, value in secrets.items():
        # OAuth2's client_secret is read by name by oauth2_client; every other secret is reached
        # through {{secrets.<connector_id>_<key>}}, so it is stored under that prefixed name.
        config["client_secret" if key == "client_secret" and config.get("auth_type") == "oauth2"
               else "{0}_{1}".format(connector_id, key)] = value
    config["template"] = {"id": template_id, "environment": environment,
                          "verified_on": template["verification"]["checked_on"]}

    variables = []
    if request.get("install_variables", True):
        for spec in template["variables"]:
            variables.append({
                "id": "{0}_{1}".format(connector_id, spec["id"]),
                "name": "{0} — {1}".format(template["provider"], spec["name"]),
                "category": spec["category"],
                "source_id": connector_id,
                "code": _render(spec["code"], params),
                "description": spec["description"],
                # dev, not prod: a starter variable is reviewed and promoted like any other.
                "status": "dev",
            })
    return {"connector": connector, "variables": variables}
