# RuleMind Mobile E2E Closeout

Generated at: `2026-07-30T16:27:37.479220+00:00`

## Summary

- Scenario pass rate: **12/12**
- Median live SDK latency: **23 ms**
- P95 live SDK latency: **24 ms**
- Max live SDK latency: **752 ms**
- Capabilities covered: auditability, callback_queueing, decisioning, explainability, review_gates, review_resume, rules, scorecards, traceability, workflow_orchestration

## Screen To Route Map

| Attached Screen | Route |
| --- | --- |
| 1. Login / Access Screen | `access` |
| 2. Experience Selection Screen | `experience_selection` |
| 3. Home Dashboard | `home_dashboard` |
| 4. Demo Scenario Hub | `scenario_hub` |
| 5. Logs & Explainability Landing | `logs_explainability` |
| 6. Admin Console Landing | `admin_console` |
| 7. Travel Flow Landing | `travel_landing` |
| 8. Trip Basics | `trip_basics` |
| 9. Travel Readiness | `travel_readiness` |
| 10. Coverage Preferences | `coverage_preferences` |
| 11. Verification & Callback Processing | `travel_processing` |
| 12. Decision Outcome | `travel_decision` |
| 13. Explainability & Audit View | `travel_audit` |
| 14. Loan Flow Landing | `loan_landing` |
| 15. Applicant Basics | `applicant_basics` |
| 16. Income & Employment | `income_employment` |
| 17. Obligations & Stability | `obligations_stability` |
| 18. Documents & Identity Inputs | `documents_identity` |
| 19. External Verification & Rule Processing | `loan_processing` |
| 20. Offer Decision | `loan_offer` |
| 21. Underwriting Explainability & Audit | `loan_audit` |
| 22. SME Flow Landing | `sme_landing` |
| 23. Business Onboarding | `business_onboarding` |
| 24. Coverage Design | `coverage_design` |
| 25. Employee Census Intake | `employee_census` |
| 26. Prior Policy & Claims Experience | `prior_policy_claims` |
| 27. Compliance Documents | `compliance_documents` |
| 28. External Verification & Parsing | `sme_processing` |
| 29. Underwriting, Compliance & Routing Evaluation | `sme_routing` |
| 30. Final Outcome | `sme_outcome` |
| 31. Full Explainability, Audit & Workflow Trace | `sme_audit` |

## Scenario Accuracy Matrix

| Scenario | Journey | Expected | Observed | Latency (ms) | Trace Steps | Pending Callbacks | Passed |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| travel_happy_path | travel_guard | approve/completed | approve/completed | 752 | 8 | 1 | True |
| travel_senior_review | travel_guard | review/paused | review/paused | 23 | 7 | 1 | True |
| travel_passport_failure | travel_guard | reject/completed | reject/completed | 22 | 8 | 1 | True |
| loan_prime_approved | instant_personal_loan | approve/completed | approve/completed | 24 | 9 | 1 | True |
| loan_borderline_review | instant_personal_loan | review/paused | review/paused | 23 | 8 | 1 | True |
| loan_high_foir_reject | instant_personal_loan | reject/completed | reject/completed | 23 | 9 | 1 | True |
| loan_fraud_review | instant_personal_loan | review/paused | review/paused | 23 | 8 | 1 | True |
| loan_kyc_reject | instant_personal_loan | reject/completed | reject/completed | 22 | 9 | 1 | True |
| sme_instant_quote | sme_underwriting | approve/completed | approve/completed | 23 | 10 | 2 | True |
| sme_claims_review | sme_underwriting | review/paused | review/paused | 23 | 8 | 1 | True |
| sme_census_failure | sme_underwriting | review/paused | review/paused | 23 | 8 | 1 | True |
| sme_compliance_hold | sme_underwriting | review/paused | review/paused | 22 | 8 | 1 | True |

## Scenario Capability Matrix

| Scenario | Journey | Capabilities Demonstrated | Resume Result |
| --- | --- | --- | --- |
| travel_happy_path | travel_guard | auditability, callback_queueing, decisioning, explainability, rules, scorecards, traceability, workflow_orchestration | n/a / n/a |
| travel_senior_review | travel_guard | auditability, callback_queueing, decisioning, explainability, review_gates, rules, scorecards, traceability, workflow_orchestration | completed / approve |
| travel_passport_failure | travel_guard | auditability, callback_queueing, decisioning, explainability, rules, scorecards, traceability, workflow_orchestration | n/a / n/a |
| loan_prime_approved | instant_personal_loan | auditability, callback_queueing, decisioning, explainability, rules, scorecards, traceability, workflow_orchestration | n/a / n/a |
| loan_borderline_review | instant_personal_loan | auditability, callback_queueing, decisioning, explainability, review_gates, rules, scorecards, traceability, workflow_orchestration | completed / approve |
| loan_high_foir_reject | instant_personal_loan | auditability, callback_queueing, decisioning, explainability, rules, scorecards, traceability, workflow_orchestration | n/a / n/a |
| loan_fraud_review | instant_personal_loan | auditability, callback_queueing, decisioning, explainability, review_gates, rules, scorecards, traceability, workflow_orchestration | completed / approve |
| loan_kyc_reject | instant_personal_loan | auditability, callback_queueing, decisioning, explainability, rules, scorecards, traceability, workflow_orchestration | n/a / n/a |
| sme_instant_quote | sme_underwriting | auditability, callback_queueing, decisioning, explainability, rules, scorecards, traceability, workflow_orchestration | n/a / n/a |
| sme_claims_review | sme_underwriting | auditability, callback_queueing, decisioning, explainability, review_gates, rules, scorecards, traceability, workflow_orchestration | completed / approve |
| sme_census_failure | sme_underwriting | auditability, callback_queueing, decisioning, explainability, review_gates, rules, scorecards, traceability, workflow_orchestration | completed / approve |
| sme_compliance_hold | sme_underwriting | auditability, callback_queueing, decisioning, explainability, review_gates, rules, scorecards, traceability, workflow_orchestration | completed / approve |

## APK Release Checklist

- [ ] `ANDROID_SAMPLE_KEYSTORE_BASE64`
- [ ] `ANDROID_SAMPLE_KEYSTORE_PASSWORD`
- [ ] `ANDROID_SAMPLE_KEY_ALIAS`
- [ ] `ANDROID_SAMPLE_KEY_PASSWORD`
- [ ] Signed `sample-app` release APK built in CI
- [ ] APK checksum and version metadata archived
- [ ] Install smoke test captured after signed build

## Residual Risks

- Device-cloud validation remains gated by external BrowserStack credentials.
- Signed release APK generation remains gated by Android signing secrets.
- Local Flutter and Android emulator execution still depends on host tool installation; CI remains the primary E2E path.
