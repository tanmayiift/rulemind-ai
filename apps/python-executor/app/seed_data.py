from copy import deepcopy


CONNECTORS = [
    {
        "id": "bureau",
        "name": "Credit Bureau",
        "icon": "bureau",
        "color": "#4d8eff",
        "description": "CIBIL / Experian",
        "schema_paths": [
            "scores[].scoreName",
            "scores[].score",
            "accounts[].accountType",
            "accounts[].currentBalance",
            "accounts[].creditLimit",
            "accounts[].paymentHistory",
            "enquiries[].enquiryDate",
        ],
        "sample_payload": {
            "scores": [{"scoreName": "CIBILTUSC3", "score": "00760"}],
            "accounts": [
                {
                    "accountType": "10",
                    "currentBalance": 7465,
                    "creditLimit": 114000,
                    "paymentHistory": "000000000000",
                }
            ],
            "enquiries": [{"enquiryDate": "20260301"}],
        },
        "is_active": True,
        "config": {"auth_type": "api_key", "base_url": "https://bureau.example.com/pull", "timeout_ms": 3000, "retries": 2},
    },
    {
        "id": "bank",
        "name": "Bank Statement",
        "icon": "bank",
        "color": "#10b981",
        "description": "Perfios / Finbox",
        "schema_paths": [
            "summary.avgBalance",
            "salary[].amount",
            "bounces[].amount",
            "emiPayments[].amount",
        ],
        "sample_payload": {
            "summary": {"avgBalance": 45200},
            "salary": [{"amount": 62000}],
            "bounces": [{"amount": 5000}],
            "emiPayments": [{"amount": 8500}],
        },
        "is_active": True,
        "config": {"auth_type": "bearer", "base_url": "https://bank.example.com/statements", "timeout_ms": 3000, "retries": 2},
    },
    {
        "id": "gst",
        "name": "GST Returns",
        "icon": "gst",
        "color": "#f5a623",
        "description": "Business filing data",
        "schema_paths": ["annualTurnover", "complianceScore", "filingStatus"],
        "sample_payload": {"annualTurnover": 4800000, "complianceScore": 92, "filingStatus": "ACTIVE"},
        "is_active": True,
        "config": {"auth_type": "oauth2", "base_url": "https://gst.example.com/returns", "timeout_ms": 3000, "retries": 2},
    },
    {
        "id": "device",
        "name": "Device / App",
        "icon": "device",
        "color": "#a78bfa",
        "description": "SDK signals",
        "schema_paths": ["lendingAppsCount", "vpnDetected", "rootDetected", "deviceAge"],
        "sample_payload": {"lendingAppsCount": 2, "vpnDetected": False, "rootDetected": False, "deviceAge": 410},
        "is_active": True,
        "config": {"auth_type": "signed_webhook", "webhook_url": "https://example.com/rulemind/device", "timeout_ms": 3000, "retries": 2},
    },
    {
        "id": "kyc",
        "name": "KYC / Identity",
        "icon": "kyc",
        "color": "#f06060",
        "description": "PAN, Aadhaar match",
        "schema_paths": ["nameMatchScore", "age", "panValid"],
        "sample_payload": {"nameMatchScore": 95, "age": 28, "panValid": True},
        "is_active": True,
        "config": {"auth_type": "basic", "base_url": "https://kyc.example.com/verify", "timeout_ms": 3000, "retries": 2},
    },
    {
        "id": "custom",
        "name": "Custom API",
        "icon": "custom",
        "color": "#64748b",
        "description": "Any JSON",
        "schema_paths": ["(user-defined)"],
        "sample_payload": {},
        "is_active": False,
        "config": {"schema_editor_enabled": True},
    },
]


VARIABLES = [
    {
        "id": "bureau_score",
        "name": "Bureau Score",
        "category": "Bureau",
        "source_id": "bureau",
        "description": "CIBIL score",
        "code": "@variable(source=\"bureau\")\ndef bureau_score(payload, variables, apis):\n    for score in payload.get(\"scores\", []):\n        if score.get(\"scoreName\") == \"CIBILTUSC3\":\n            return int(score.get(\"score\", 0))\n    return 0\n",
        "status": "prod",
        "seed_value": 760,
    },
    {
        "id": "active_accounts",
        "name": "Active Accounts",
        "category": "Bureau",
        "source_id": "bureau",
        "description": "Open accounts",
        "code": "@variable(source=\"bureau\")\ndef active_accounts(payload, variables, apis):\n    return sum(1 for account in payload.get(\"accounts\", []) if not account.get(\"dateClosed\"))\n",
        "status": "prod",
        "seed_value": 4,
    },
    {
        "id": "credit_util",
        "name": "Credit Util %",
        "category": "Bureau",
        "source_id": "bureau",
        "description": "CC balance / limit",
        "code": "@variable(source=\"bureau\")\ndef credit_util(payload, variables, apis):\n    cards = [account for account in payload.get(\"accounts\", []) if account.get(\"accountType\") == \"10\"]\n    limit_total = sum(account.get(\"creditLimit\", 0) for account in cards)\n    balance_total = sum(account.get(\"currentBalance\", 0) for account in cards)\n    return round((balance_total / limit_total) * 100, 1) if limit_total else 0\n",
        "status": "prod",
        "seed_value": 5.2,
    },
    {
        "id": "dpd_count",
        "name": "DPD 30+",
        "category": "Bureau",
        "source_id": "bureau",
        "description": "Past due count",
        "code": "@variable(source=\"bureau\")\ndef dpd_count(payload, variables, apis):\n    count = 0\n    for account in payload.get(\"accounts\", []):\n        history = account.get(\"paymentHistory\", \"\")\n        for index in range(0, len(history), 3):\n            chunk = history[index:index + 3]\n            if chunk.isdigit() and int(chunk) >= 30:\n                count += 1\n    return count\n",
        "status": "prod",
        "seed_value": 0,
    },
    {
        "id": "enquiry_6m",
        "name": "Enquiries 6M",
        "category": "Bureau",
        "source_id": "bureau",
        "description": "Recent hard pulls",
        "code": "@variable(source=\"bureau\")\ndef enquiry_6m(payload, variables, apis):\n    return len(payload.get(\"enquiries\", []))\n",
        "status": "prod",
        "seed_value": 2,
    },
    {
        "id": "avg_balance",
        "name": "Avg Balance",
        "category": "Banking",
        "source_id": "bank",
        "description": "Monthly average balance",
        "code": "@variable(source=\"bank\")\ndef avg_balance(payload, variables, apis):\n    return payload.get(\"summary\", {}).get(\"avgBalance\", 0)\n",
        "status": "uat",
        "seed_value": 45200,
    },
    {
        "id": "salary",
        "name": "Latest Salary",
        "category": "Banking",
        "source_id": "bank",
        "description": "Salary credit",
        "code": "@variable(source=\"bank\")\ndef salary(payload, variables, apis):\n    salaries = payload.get(\"salary\", [])\n    return salaries[0].get(\"amount\", 0) if salaries else 0\n",
        "status": "uat",
        "seed_value": 62000,
    },
    {
        "id": "bounces",
        "name": "Bounce Count",
        "category": "Banking",
        "source_id": "bank",
        "description": "Cheque bounces",
        "code": "@variable(source=\"bank\")\ndef bounces(payload, variables, apis):\n    return len(payload.get(\"bounces\", []))\n",
        "status": "uat",
        "seed_value": 1,
    },
    {
        "id": "foir",
        "name": "FOIR %",
        "category": "Banking",
        "source_id": "bank",
        "description": "Obligations / income",
        "code": "@variable(source=\"bank\")\ndef foir(payload, variables, apis):\n    salaries = payload.get(\"salary\", [])\n    income = salaries[0].get(\"amount\", 0) if salaries else 0\n    obligations = sum(item.get(\"amount\", 0) for item in payload.get(\"emiPayments\", []))\n    return round((obligations / income) * 100, 1) if income else 0\n",
        "status": "uat",
        "seed_value": 13.7,
    },
    {
        "id": "gst_turnover",
        "name": "Turnover",
        "category": "Business",
        "source_id": "gst",
        "description": "Annual turnover",
        "code": "@variable(source=\"gst\")\ndef gst_turnover(payload, variables, apis):\n    return payload.get(\"annualTurnover\", 0)\n",
        "status": "dev",
        "seed_value": 4800000,
    },
    {
        "id": "gst_compliance",
        "name": "GST Compliance",
        "category": "Business",
        "source_id": "gst",
        "description": "Filing score",
        "code": "@variable(source=\"gst\")\ndef gst_compliance(payload, variables, apis):\n    return payload.get(\"complianceScore\", 0)\n",
        "status": "dev",
        "seed_value": 92,
    },
    {
        "id": "lending_apps",
        "name": "Lending Apps",
        "category": "Device",
        "source_id": "device",
        "description": "App count",
        "code": "@variable(source=\"device\")\ndef lending_apps(payload, variables, apis):\n    return payload.get(\"lendingAppsCount\", 0)\n",
        "status": "dev",
        "seed_value": 2,
    },
    {
        "id": "device_risk",
        "name": "Device Risk",
        "category": "Device",
        "source_id": "device",
        "description": "VPN / root flag",
        "code": "@variable(source=\"device\")\ndef device_risk(payload, variables, apis):\n    return 1 if payload.get(\"vpnDetected\") or payload.get(\"rootDetected\") else 0\n",
        "status": "dev",
        "seed_value": 0,
    },
    {
        "id": "name_match",
        "name": "Name Match",
        "category": "Identity",
        "source_id": "kyc",
        "description": "KYC score",
        "code": "@variable(source=\"kyc\")\ndef name_match(payload, variables, apis):\n    return payload.get(\"nameMatchScore\", 0)\n",
        "status": "dev",
        "seed_value": 95,
    },
    {
        "id": "age",
        "name": "Age",
        "category": "Identity",
        "source_id": "kyc",
        "description": "Applicant age",
        "code": "@variable(source=\"kyc\")\ndef age(payload, variables, apis):\n    return payload.get(\"age\", 0)\n",
        "status": "dev",
        "seed_value": 28,
    },
]


RULES = [
    {
        "id": "r1",
        "name": "PL Score Gate (Bureau+Bank+KYC)",
        "status": "prod",
        "nodes": [
            {"id": "n1", "type": "condition", "variable": "bureau_score", "operator": ">=", "value": "700", "label": "Condition"},
            {"id": "n2", "type": "and", "label": "AND"},
            {"id": "n3", "type": "condition", "variable": "avg_balance", "operator": ">=", "value": "20000", "label": "Condition"},
            {"id": "n4", "type": "and", "label": "AND"},
            {"id": "n5", "type": "condition", "variable": "age", "operator": ">=", "value": "21", "label": "Condition"},
            {"id": "n6", "type": "approve", "label": "Approve"},
        ],
    },
    {
        "id": "r2",
        "name": "Risk Block (Bureau+Bank+Device)",
        "status": "uat",
        "nodes": [
            {"id": "n1", "type": "condition", "variable": "dpd_count", "operator": ">", "value": "0", "label": "Condition"},
            {"id": "n2", "type": "or", "label": "OR"},
            {"id": "n3", "type": "condition", "variable": "bounces", "operator": ">", "value": "2", "label": "Condition"},
            {"id": "n4", "type": "or", "label": "OR"},
            {"id": "n5", "type": "condition", "variable": "device_risk", "operator": "==", "value": "1", "label": "Condition"},
            {"id": "n6", "type": "reject", "label": "Reject"},
        ],
    },
]


SCORECARDS = [
    {
        "id": "sc1",
        "name": "PL Eligibility Score",
        "base_score": 300,
        "max_score": 900,
        "status": "prod",
        "bins": [
            {
                "variable_id": "bureau_score",
                "ranges": [
                    {"min": 0, "max": 649, "points": -80},
                    {"min": 650, "max": 719, "points": 0},
                    {"min": 720, "max": 780, "points": 40},
                    {"min": 781, "max": 900, "points": 80},
                ],
            },
            {
                "variable_id": "avg_balance",
                "ranges": [
                    {"min": 0, "max": 10000, "points": -30},
                    {"min": 10001, "max": 30000, "points": 10},
                    {"min": 30001, "max": 999999999, "points": 40},
                ],
            },
            {
                "variable_id": "bounces",
                "ranges": [
                    {"min": 0, "max": 0, "points": 30},
                    {"min": 1, "max": 2, "points": -10},
                    {"min": 3, "max": 999, "points": -50},
                ],
            },
        ],
    }
]


POLICIES = [
    {
        "id": "policy_pl_underwriting",
        "name": "PL Underwriting",
        "status": "uat",
        "steps": [
            {"id": "step_1", "type": "connector", "ref_id": "bureau", "label": "Bureau Pull"},
            {"id": "step_2", "type": "connector", "ref_id": "bank", "label": "Bank Analysis"},
            {"id": "step_3", "type": "connector", "ref_id": "kyc", "label": "KYC Check"},
            {"id": "step_4", "type": "rule", "ref_id": "r1", "label": "Score Gate"},
            {"id": "step_5", "type": "scorecard", "ref_id": "sc1", "label": "PL Scorecard"},
            {"id": "step_6", "type": "rule", "ref_id": "r2", "label": "Risk Block"},
            {"id": "step_7", "type": "outcome", "ref_id": "approve", "label": "Decision"},
        ],
    }
]


DEFAULT_SETTINGS = {
    "api_base_url": "http://localhost:8080",
    "auth_config": {"method": "bearer", "token": ""},
    "engine_config": {"python_version": "3.11", "timeout_ms": 2000, "memory_mb": 128},
    "source_defaults": {"default_format": "json", "batch_support": True, "active_sources": ["bureau", "bank", "gst", "device", "kyc"]},
    "audit_retention_days": 90,
    "theme_mode": "light",
}


def connector_map():
    return {item["id"]: deepcopy(item) for item in CONNECTORS}


def variable_templates():
    return deepcopy(VARIABLES)
