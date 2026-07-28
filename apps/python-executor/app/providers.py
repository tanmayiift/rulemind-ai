"""Built-in provider templates for workflow API steps.

Each template is a ready-to-drop `action` step config (Postman-style). Some use
free/open APIs that work with no credentials; the fintech ones are templated with
`{{secrets.*}}` placeholders and declare the credentials they need. The generic
`http_request` template is the Postman-style "call any API" step.

Templating: `{{payload.custom.<field>}}`, `{{variables.<id>}}`, `{{secrets.<key>}}`,
`{{outcome}}`, `{{execution_id}}` are resolved at execution time.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

PROVIDER_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "http_request",
        "name": "HTTP Request",
        "category": "generic",
        "description": "Call any REST API — configure method, headers, and body (Postman-style).",
        "credentials": [],
        "action": {"url": "https://", "method": "GET", "headers": {}, "bodyTemplate": {}, "timeoutMs": 5000, "retries": 1},
    },
    # --- Free / open APIs (work out of the box, no key) ---------------------
    {
        "id": "ip_geolocation",
        "name": "IP Geolocation (ipapi.co)",
        "category": "device",
        "description": "Resolve geolocation from an IP address. No API key required.",
        "credentials": [],
        "action": {"url": "https://ipapi.co/{{payload.custom.ip}}/json/", "method": "GET", "timeoutMs": 4000},
    },
    {
        "id": "uk_postcode",
        "name": "UK Postcode lookup (postcodes.io)",
        "category": "address",
        "description": "Validate and enrich a UK postcode. No API key required.",
        "credentials": [],
        "action": {"url": "https://api.postcodes.io/postcodes/{{payload.custom.postcode}}", "method": "GET", "timeoutMs": 4000},
    },
    {
        "id": "exchange_rates",
        "name": "FX rates (open.er-api.com)",
        "category": "reference",
        "description": "Fetch live exchange rates for a base currency. No API key required.",
        "credentials": [],
        "action": {"url": "https://open.er-api.com/v6/latest/{{payload.custom.currency}}", "method": "GET"},
    },
    # --- Fintech providers (need credentials; templated) -------------------
    {
        "id": "credit_bureau",
        "name": "Credit Bureau report",
        "category": "bureau",
        "description": "Pull a credit bureau report. Long-running — configured async by default.",
        "credentials": ["bureau_token"],
        "action": {
            "url": "https://bureau.example.com/v1/report",
            "method": "POST",
            "mode": "async",
            "headers": {"Authorization": "Bearer {{secrets.bureau_token}}"},
            "bodyTemplate": {"pan": "{{payload.custom.pan}}", "consent": True},
            "timeoutMs": 8000,
        },
    },
    {
        "id": "kyc_aadhaar_pan",
        "name": "KYC — Aadhaar/PAN name match",
        "category": "kyc",
        "description": "Verify that the Aadhaar and PAN names match above a confidence threshold.",
        "credentials": ["kyc_api_key"],
        "action": {
            "url": "https://kyc.example.com/v1/name-match",
            "method": "POST",
            "headers": {"x-api-key": "{{secrets.kyc_api_key}}"},
            "bodyTemplate": {"aadhaar_name": "{{payload.custom.aadhaar_name}}", "pan_name": "{{payload.custom.pan_name}}"},
        },
    },
    {
        "id": "liveness",
        "name": "Liveness / face match",
        "category": "kyc",
        "description": "Run a liveness + face-match check on a selfie. Long-running — async by default.",
        "credentials": ["liveness_api_key"],
        "action": {
            "url": "https://liveness.example.com/v1/check",
            "method": "POST",
            "mode": "async",
            "headers": {"x-api-key": "{{secrets.liveness_api_key}}"},
            "bodyTemplate": {"selfie_url": "{{payload.custom.selfie_url}}"},
        },
    },
    {
        "id": "aml_screening",
        "name": "AML / sanctions screening",
        "category": "aml",
        "description": "Screen a subject against sanctions and PEP lists.",
        "credentials": ["aml_api_key"],
        "action": {
            "url": "https://aml.example.com/v1/screen",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{secrets.aml_api_key}}"},
            "bodyTemplate": {"name": "{{payload.custom.name}}", "dob": "{{payload.custom.dob}}"},
        },
    },
    {
        "id": "device_fingerprint",
        "name": "Device fingerprint risk",
        "category": "device",
        "description": "Assess device risk (rooted/emulated, velocity) from a device signal.",
        "credentials": ["device_api_key"],
        "action": {
            "url": "https://device.example.com/v1/risk",
            "method": "POST",
            "headers": {"x-api-key": "{{secrets.device_api_key}}"},
            "bodyTemplate": {"device_id": "{{payload.custom.device_id}}"},
        },
    },
]

_BY_ID = {template["id"]: template for template in PROVIDER_TEMPLATES}


def list_providers(category: Optional[str] = None) -> List[Dict[str, Any]]:
    if category:
        return [t for t in PROVIDER_TEMPLATES if t["category"] == category]
    return list(PROVIDER_TEMPLATES)


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    return _BY_ID.get(provider_id)
