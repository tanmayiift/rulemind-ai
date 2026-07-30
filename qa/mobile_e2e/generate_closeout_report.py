from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "apps" / "python-executor"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # this QA report decides against the sample lending inventory

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


SCREEN_ROUTE_MAP = [
    ("1. Login / Access Screen", "access"),
    ("2. Experience Selection Screen", "experience_selection"),
    ("3. Home Dashboard", "home_dashboard"),
    ("4. Demo Scenario Hub", "scenario_hub"),
    ("5. Logs & Explainability Landing", "logs_explainability"),
    ("6. Admin Console Landing", "admin_console"),
    ("7. Travel Flow Landing", "travel_landing"),
    ("8. Trip Basics", "trip_basics"),
    ("9. Travel Readiness", "travel_readiness"),
    ("10. Coverage Preferences", "coverage_preferences"),
    ("11. Verification & Callback Processing", "travel_processing"),
    ("12. Decision Outcome", "travel_decision"),
    ("13. Explainability & Audit View", "travel_audit"),
    ("14. Loan Flow Landing", "loan_landing"),
    ("15. Applicant Basics", "applicant_basics"),
    ("16. Income & Employment", "income_employment"),
    ("17. Obligations & Stability", "obligations_stability"),
    ("18. Documents & Identity Inputs", "documents_identity"),
    ("19. External Verification & Rule Processing", "loan_processing"),
    ("20. Offer Decision", "loan_offer"),
    ("21. Underwriting Explainability & Audit", "loan_audit"),
    ("22. SME Flow Landing", "sme_landing"),
    ("23. Business Onboarding", "business_onboarding"),
    ("24. Coverage Design", "coverage_design"),
    ("25. Employee Census Intake", "employee_census"),
    ("26. Prior Policy & Claims Experience", "prior_policy_claims"),
    ("27. Compliance Documents", "compliance_documents"),
    ("28. External Verification & Parsing", "sme_processing"),
    ("29. Underwriting, Compliance & Routing Evaluation", "sme_routing"),
    ("30. Final Outcome", "sme_outcome"),
    ("31. Full Explainability, Audit & Workflow Trace", "sme_audit"),
]


@dataclass
class ScenarioResult:
    scenario_id: str
    journey_id: str
    title: str
    expected_outcome: str
    observed_outcome: str
    expected_status: str
    observed_status: str
    latency_ms: int
    trace_steps: int
    pending_callbacks: int
    execution_id: str
    request_id: str
    capabilities: list[str]
    passed: bool
    resumed_status: str | None = None
    resumed_outcome: str | None = None


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def collect_report() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tempdir:
        database_path = os.path.join(tempdir, "rulemind-mobile-closeout.db")
        app_main.storage = Storage(path=database_path)
        client = TestClient(app_main.app)
        headers = {"x-api-key": app_main.storage.default_api_key or ""}

        try:
            manifest_response = client.post("/api/mobile/v1/auth/demo")
            manifest_response.raise_for_status()
            manifest = manifest_response.json()["experienceManifest"]
            journeys = {item["id"]: item for item in manifest["journeys"]}

            scenario_results: list[ScenarioResult] = []
            covered_capabilities: set[str] = set()
            latencies: list[int] = []

            for scenario in manifest["scenarios"]:
                journey = journeys[scenario["journeyId"]]
                decide_response = client.post(
                    "/sdk/v1/decide",
                    headers=headers,
                    json={
                        "policyId": journey["policyId"],
                        "requestId": f"closeout-{scenario['id']}",
                        "userId": "qa-closeout",
                        "payload": scenario["payload"],
                    },
                )
                decide_response.raise_for_status()
                body = decide_response.json()
                latencies.append(int(body.get("latencyMs") or 0))
                queued_callbacks = [
                    item
                    for item in body.get("actionResults") or []
                    if isinstance(item, dict) and (item.get("queued") or item.get("status") == "queued")
                ]
                pending_callbacks = [
                    item
                    for item in body.get("pendingOperations") or []
                    if isinstance(item, dict) and item.get("status") != "delivered"
                ]

                trace_step_types = [
                    ((item.get("step") or {}).get("type") or "").strip()
                    for item in body.get("trace") or []
                    if isinstance(item, dict)
                ]
                scenario_capabilities = {
                    "decisioning",
                    "traceability" if body.get("trace") else "",
                    "auditability" if body.get("auditSummary") else "",
                    "explainability" if body.get("explainability") else "",
                    "rules" if "rule" in trace_step_types else "",
                    "scorecards" if "scorecard" in trace_step_types else "",
                    "workflow_orchestration" if trace_step_types else "",
                    "callback_queueing" if pending_callbacks or queued_callbacks else "",
                    "review_gates" if body.get("reviewTask") else "",
                }
                covered_capabilities.update(item for item in scenario_capabilities if item)

                resumed_status: str | None = None
                resumed_outcome: str | None = None
                if body.get("status") == "paused" and body.get("executionId"):
                    resume_response = client.post(
                        f"/sdk/v1/executions/{body['executionId']}/resume",
                        headers=headers,
                        json={
                            "decision": "approve",
                            "reviewerId": "qa-closeout",
                            "response": {
                                "medical_clearance_note": "Approved by automation",
                                "approved_amount_inr": 50000,
                                "underwriter_note": "Approved by automation",
                            },
                        },
                    )
                    resume_response.raise_for_status()
                    resumed = resume_response.json()
                    resumed_status = resumed.get("status")
                    resumed_outcome = resumed.get("outcome")
                    covered_capabilities.add("review_resume")

                scenario_results.append(
                    ScenarioResult(
                        scenario_id=scenario["id"],
                        journey_id=scenario["journeyId"],
                        title=scenario["title"],
                        expected_outcome=scenario["expectedOutcome"],
                        observed_outcome=body["outcome"],
                        expected_status=scenario["expectedStatus"],
                        observed_status=body["status"],
                        latency_ms=int(body.get("latencyMs") or 0),
                        trace_steps=len(body.get("trace") or []),
                        pending_callbacks=max(len(pending_callbacks), len(queued_callbacks)),
                        execution_id=body.get("executionId") or "",
                        request_id=body.get("requestId") or "",
                        capabilities=sorted(item for item in scenario_capabilities if item),
                        passed=body["outcome"] == scenario["expectedOutcome"] and body["status"] == scenario["expectedStatus"],
                        resumed_status=resumed_status,
                        resumed_outcome=resumed_outcome,
                    )
                )

            passed_count = sum(1 for item in scenario_results if item.passed)
            latency_summary = {
                "medianMs": int(statistics.median(latencies)) if latencies else 0,
                "p95Ms": percentile(latencies, 0.95),
                "maxMs": max(latencies) if latencies else 0,
            }

            return {
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "manifestVersion": manifest.get("version"),
                "tenantId": str(app_main.storage.default_tenant_id),
                "screenRouteMap": [{"screen": screen, "route": route} for screen, route in SCREEN_ROUTE_MAP],
                "scenarioResults": [asdict(item) for item in scenario_results],
                "scenarioPassRate": {
                    "passed": passed_count,
                    "total": len(scenario_results),
                },
                "latencySummary": latency_summary,
                "capabilitiesCovered": sorted(covered_capabilities),
                "residualRisks": [
                    "Device-cloud validation remains gated by external BrowserStack credentials.",
                    "Signed release APK generation remains gated by Android signing secrets.",
                    "Local Flutter and Android emulator execution still depends on host tool installation; CI remains the primary E2E path.",
                ],
            }
        finally:
            client.close()


def render_markdown(report: dict[str, Any]) -> str:
    scenario_rows = []
    for item in report["scenarioResults"]:
        scenario_rows.append(
            "| {scenario_id} | {journey_id} | {expected_outcome}/{expected_status} | {observed_outcome}/{observed_status} | {latency_ms} | {trace_steps} | {pending_callbacks} | {passed} |".format(
                **item
            )
        )

    capability_rows = []
    for item in report["scenarioResults"]:
        capability_rows.append(
            "| {scenario_id} | {journey_id} | {capabilities} | {resumed_status} / {resumed_outcome} |".format(
                scenario_id=item["scenario_id"],
                journey_id=item["journey_id"],
                capabilities=", ".join(item["capabilities"]),
                resumed_status=item["resumed_status"] or "n/a",
                resumed_outcome=item["resumed_outcome"] or "n/a",
            )
        )

    screen_rows = [f"| {screen} | `{route}` |" for screen, route in SCREEN_ROUTE_MAP]
    risks = "\n".join(f"- {risk}" for risk in report["residualRisks"])

    return f"""# RuleMind Mobile E2E Closeout

Generated at: `{report["generatedAt"]}`

## Summary

- Scenario pass rate: **{report["scenarioPassRate"]["passed"]}/{report["scenarioPassRate"]["total"]}**
- Median live SDK latency: **{report["latencySummary"]["medianMs"]} ms**
- P95 live SDK latency: **{report["latencySummary"]["p95Ms"]} ms**
- Max live SDK latency: **{report["latencySummary"]["maxMs"]} ms**
- Capabilities covered: {", ".join(report["capabilitiesCovered"])}

## Screen To Route Map

| Attached Screen | Route |
| --- | --- |
{chr(10).join(screen_rows)}

## Scenario Accuracy Matrix

| Scenario | Journey | Expected | Observed | Latency (ms) | Trace Steps | Pending Callbacks | Passed |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
{chr(10).join(scenario_rows)}

## Scenario Capability Matrix

| Scenario | Journey | Capabilities Demonstrated | Resume Result |
| --- | --- | --- | --- |
{chr(10).join(capability_rows)}

## APK Release Checklist

- [ ] `ANDROID_SAMPLE_KEYSTORE_BASE64`
- [ ] `ANDROID_SAMPLE_KEYSTORE_PASSWORD`
- [ ] `ANDROID_SAMPLE_KEY_ALIAS`
- [ ] `ANDROID_SAMPLE_KEY_PASSWORD`
- [ ] Signed `sample-app` release APK built in CI
- [ ] APK checksum and version metadata archived
- [ ] Install smoke test captured after signed build

## Residual Risks

{risks}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the RuleMind mobile E2E closeout report.")
    parser.add_argument("--output-md", default=str(REPO_ROOT / "qa" / "results" / "mobile-e2e-closeout.md"))
    parser.add_argument("--output-json", default=str(REPO_ROOT / "qa" / "results" / "mobile-e2e-closeout.json"))
    args = parser.parse_args()

    report = collect_report()
    output_md = Path(args.output_md)
    output_json = Path(args.output_json)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(render_markdown(report), encoding="utf-8")
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
