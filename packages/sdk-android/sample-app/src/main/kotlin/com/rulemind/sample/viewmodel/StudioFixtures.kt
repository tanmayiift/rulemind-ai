package com.rulemind.sample.viewmodel

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.CompiledRule
import com.rulemind.core.models.CompiledVariable
import com.rulemind.core.models.Experiment
import com.rulemind.core.models.Instruction
import com.rulemind.core.models.Policy
import com.rulemind.core.models.PolicyStep
import com.rulemind.core.models.RuleTreeNode
import com.rulemind.core.models.ScoreFactor
import com.rulemind.core.models.ScoreRange
import com.rulemind.core.models.Scorecard

object StudioFixtures {
    fun demoContent(): StudioContent = StudioContent(
        appName = "RuleMind Experience Studio",
        tagline = "Decisioning journeys, orchestration, callbacks, and explainability in one mobile studio.",
        version = "2026.04.01",
        sections = listOf(
            StudioShellSection("home_dashboard", "Home", "home"),
            StudioShellSection("travel_guard", "Travel", "flight"),
            StudioShellSection("instant_personal_loan", "Loan", "account_balance"),
            StudioShellSection("sme_underwriting", "SME", "business"),
            StudioShellSection("scenario_hub", "Scenarios", "science"),
            StudioShellSection("logs_explainability", "Logs", "terminal"),
            StudioShellSection("admin_console", "Admin", "settings"),
        ),
        journeys = listOf(travelJourney(), loanJourney(), smeJourney()),
        scenarios = travelScenarios() + loanScenarios() + smeScenarios(),
        adminTools = listOf(
            StudioAdminTool("threshold_controls", "Rule Thresholds", "Adjust sensitive limit parameters and preview decision impact."),
            StudioAdminTool("workflow_paths", "Workflow Paths", "Inspect routing branches, review-gate conditions, and fallbacks."),
            StudioAdminTool("callback_simulator", "Callback Simulator", "Trigger mock external events and queued callback flows."),
            StudioAdminTool("scenario_controls", "Scenario Controls", "Inject deterministic personas and edge-case payloads."),
            StudioAdminTool("decision_preview", "Decision Preview", "Run dry-run previews before changes are promoted."),
            StudioAdminTool("test_harness", "Audit & Test Harness", "Inspect traces, action logs, and explainability surfaces."),
        ),
        adminEntities = listOf(
            entity(
                id = "connectors",
                title = "Connectors",
                listPath = "/api/v1/connectors",
                detailPath = "/api/v1/connectors/{id}",
                createPath = "/api/v1/connectors",
                updatePath = "/api/v1/connectors/{id}",
                supportsCreate = true,
                supportsDelete = true,
                fields = listOf(
                    text("name", "Name", required = true),
                    text("icon", "Icon"),
                    text("color", "Color"),
                    text("description", "Description"),
                    json("schema_paths", "Schema Paths"),
                    json("sample_payload", "Sample Payload"),
                    bool("is_active", "Active"),
                    json("config", "Configuration"),
                ),
            ),
            entity(
                id = "variables",
                title = "Variables",
                listPath = "/api/v1/variables",
                detailPath = "/api/v1/variables/{id}",
                createPath = "/api/v1/variables",
                updatePath = "/api/v1/variables/{id}",
                testPath = "/api/v1/variables/{id}/test",
                supportsCreate = true,
                supportsDelete = true,
                supportsPromote = true,
                fields = listOf(
                    text("name", "Name", required = true),
                    text("category", "Category", required = true),
                    text("source_id", "Source ID", required = true),
                    choice("status", "Status", listOf("dev", "uat", "prod"), required = true),
                    text("description", "Description"),
                    code("code", "Code", required = true),
                ),
            ),
            entity(
                id = "rules",
                title = "Rules",
                listPath = "/api/v1/rules",
                detailPath = "/api/v1/rules/{id}",
                createPath = "/api/v1/rules",
                updatePath = "/api/v1/rules/{id}",
                testPath = "/api/v1/rules/{id}/test",
                supportsCreate = true,
                supportsDelete = true,
                supportsPromote = true,
                fields = listOf(
                    text("name", "Name", required = true),
                    choice("ruleFormat", "Rule Format", listOf("v1", "v2"), required = true),
                    choice("status", "Status", listOf("dev", "uat", "prod"), required = true),
                    json("nodes", "Nodes"),
                    json("tree", "Tree"),
                ),
            ),
            entity(
                id = "scorecards",
                title = "Scorecards",
                listPath = "/api/v1/scorecards",
                detailPath = "/api/v1/scorecards/{id}",
                createPath = "/api/v1/scorecards",
                updatePath = "/api/v1/scorecards/{id}",
                testPath = "/api/v1/scorecards/{id}/test",
                supportsCreate = true,
                supportsDelete = true,
                supportsPromote = true,
                fields = listOf(
                    text("name", "Name", required = true),
                    number("base_score", "Base Score", required = true),
                    number("max_score", "Max Score", required = true),
                    choice("status", "Status", listOf("dev", "uat", "prod"), required = true),
                    json("bins", "Bins"),
                ),
            ),
            entity(
                id = "policies",
                title = "Policies",
                listPath = "/api/v1/policies",
                detailPath = "/api/v1/policies/{id}",
                createPath = "/api/v1/policies",
                updatePath = "/api/v1/policies/{id}",
                testPath = "/api/v1/policies/{id}/execute",
                supportsCreate = true,
                supportsDelete = true,
                supportsPromote = true,
                fields = listOf(
                    text("name", "Name", required = true),
                    choice("status", "Status", listOf("dev", "uat", "prod"), required = true),
                    text("defaultOutcome", "Default Outcome"),
                    json("steps", "Steps"),
                ),
            ),
            entity(
                id = "experiments",
                title = "Experiments",
                listPath = "/api/v1/experiments",
                detailPath = "/api/v1/experiments/{id}",
                createPath = "/api/v1/experiments",
                updatePath = "/api/v1/experiments/{id}",
                supportsCreate = true,
                supportsDelete = true,
                fields = listOf(
                    text("name", "Name", required = true),
                    text("description", "Description"),
                    text("target_policy_id", "Target Policy ID"),
                    choice("status", "Status", listOf("draft", "running", "paused", "stopped"), required = true),
                    json("variants", "Variants"),
                ),
            ),
            entity(
                id = "webhooks",
                title = "Webhooks",
                listPath = "/api/v1/webhooks",
                detailPath = "/api/v1/webhooks/{id}",
                createPath = "/api/v1/webhooks",
                updatePath = "/api/v1/webhooks/{id}",
                supportsCreate = true,
                supportsDelete = true,
                fields = listOf(
                    text("policy_id", "Policy ID", required = true),
                    bool("is_active", "Active"),
                    text("secret", "Secret"),
                    json("payload_mapping", "Payload Mapping"),
                ),
            ),
            entity(
                id = "schedules",
                title = "Schedules",
                listPath = "/api/v1/schedules",
                detailPath = "/api/v1/schedules/{id}",
                createPath = "/api/v1/schedules",
                updatePath = "/api/v1/schedules/{id}",
                supportsCreate = true,
                supportsDelete = true,
                fields = listOf(
                    text("policy_id", "Policy ID", required = true),
                    text("cron_expression", "Cron Expression", required = true),
                    bool("is_active", "Active"),
                    json("payload_source", "Payload Source"),
                    json("config", "Configuration"),
                ),
            ),
            entity(
                id = "settings",
                title = "Settings",
                listPath = "/api/v1/settings",
                detailPath = "/api/v1/settings",
                updatePath = "/api/v1/settings",
                fields = listOf(
                    text("api_base_url", "API Base URL"),
                    json("auth_config", "Auth Configuration"),
                    json("engine_config", "Engine Configuration"),
                    json("source_defaults", "Source Defaults"),
                    number("audit_retention_days", "Audit Retention Days"),
                    choice("theme_mode", "Theme Mode", listOf("light", "dark")),
                ),
            ),
        ),
        logMetrics = listOf(
            StudioMetric("Total flows executed", "128,492", "+12% vs last week"),
            StudioMetric("Active rule groups", "84", "All systems active"),
            StudioMetric("Callback success", "99.9%", "2 failures today"),
            StudioMetric("Decision avg time", "42ms", "Optimal performance"),
        ),
    )

    fun demoBundle(): Bundle = Bundle(
        bundleVersion = 7,
        bundleId = "rulemind-mobile-studio",
        tenantId = "demo-tenant",
        compiledAt = "2026-04-01T00:00:00Z",
        expiresAt = "2030-12-31T23:59:59Z",
        variables = listOf(
            variable("travel_passport_months_valid", "travel", "Passport Validity", "passport_months_valid"),
            variable("travel_visa_ready", "travel", "Visa Ready", "visa_ready"),
            variable("travel_destination_risk", "travel", "Destination Risk", "destination_risk"),
            variable("travel_primary_age", "travel", "Primary Age", "primary_traveller_age"),
            variable("travel_medical_flag_value", "travel", "Medical Flag", "medical_flag_value"),
            variable("travel_trip_cost_inr", "travel", "Trip Cost", "trip_cost_inr"),
            variable("travel_support_matrix_ready", "travel", "Support Matrix Ready", "support_matrix_ready"),
            variable("loan_bureau_score", "loan", "Bureau Score", "bureau_score"),
            variable("loan_avg_balance_inr", "loan", "Average Balance", "avg_balance_inr"),
            variable("loan_dti_ratio", "loan", "DTI Ratio", "dti_ratio"),
            variable("loan_pan_verified_flag", "loan", "PAN Verified", "pan_verified_flag"),
            variable("loan_geo_consistency_flag", "loan", "Geo Consistency", "geo_consistency_flag"),
            variable("loan_liveness_score", "loan", "Liveness Score", "liveness_score"),
            variable("sme_gst_compliance_score", "sme", "GST Compliance", "gst_compliance_score"),
            variable("sme_census_quality_score", "sme", "Census Quality", "census_quality_score"),
            variable("sme_loss_ratio_pct", "sme", "Loss Ratio", "loss_ratio_pct"),
            variable("sme_growth_rate_pct", "sme", "Growth Rate", "growth_rate_pct"),
            variable("sme_sanctions_flag", "sme", "Sanctions Flag", "sanctions_flag"),
            variable("sme_ubo_docs_ready_flag", "sme", "UBO Ready", "ubo_docs_ready_flag"),
            variable("sme_claims_disclosed_flag", "sme", "Claims Disclosed", "claims_disclosed_flag"),
        ),
        rules = listOf(
            rule("rule_travel_passport_gate", "Travel Passport Gate", "approve", "reject", listOf(condition("travel_passport_months_valid", ">=", 6))),
            rule(
                "rule_travel_destination_gate",
                "Travel Destination Gate",
                "pass",
                "review",
                listOf(
                    condition("travel_destination_risk", "<=", 2),
                    condition("travel_support_matrix_ready", ">=", 1),
                ),
            ),
            rule(
                "rule_travel_profile_gate",
                "Travel Profile Gate",
                "pass",
                "review",
                listOf(
                    condition("travel_visa_ready", ">=", 1),
                    condition("travel_medical_flag_value", "==", 0),
                    condition("travel_primary_age", "<=", 70),
                ),
            ),
            rule("rule_loan_bureau_gate", "Loan Bureau Gate", "approve", "review", listOf(condition("loan_bureau_score", ">=", 700))),
            rule(
                "rule_loan_kyc_gate",
                "Loan KYC Gate",
                "pass",
                "reject",
                listOf(
                    condition("loan_pan_verified_flag", ">=", 1),
                    condition("loan_liveness_score", ">=", 90),
                ),
            ),
            rule("rule_loan_geo_gate", "Loan Geo Gate", "pass", "review", listOf(condition("loan_geo_consistency_flag", "==", 0))),
            rule("rule_loan_affordability_gate", "Loan Affordability Gate", "pass", "reject", listOf(condition("loan_dti_ratio", "<=", 0.45))),
            rule(
                "rule_sme_compliance_gate",
                "SME Compliance Gate",
                "approve",
                "review",
                listOf(
                    condition("sme_sanctions_flag", "==", 0),
                    condition("sme_ubo_docs_ready_flag", ">=", 1),
                ),
            ),
            rule("rule_sme_census_gate", "SME Census Gate", "pass", "review", listOf(condition("sme_census_quality_score", ">=", 85))),
            rule(
                "rule_sme_claims_gate",
                "SME Claims Gate",
                "pass",
                "review",
                listOf(
                    condition("sme_claims_disclosed_flag", ">=", 1),
                    condition("sme_loss_ratio_pct", "<=", 35),
                ),
            ),
            rule("rule_sme_growth_gate", "SME Growth Gate", "pass", "review", listOf(condition("sme_growth_rate_pct", "<=", 50))),
        ),
        scorecards = listOf(
            scorecard(
                "sc_travel_fit",
                "Travel Fit Score",
                500,
                listOf(
                    factor("travel_destination_risk", listOf(range(0, 1, 70), range(2, 2, 20), range(3, 9, -60))),
                    factor("travel_trip_cost_inr", listOf(range(0, 99999, 20), range(100000, 199999, 50), range(200000, 999999999, 30))),
                    factor("travel_primary_age", listOf(range(18, 60, 40), range(61, 74, -10), range(75, 120, -60))),
                ),
            ),
            scorecard(
                "sc_loan_risk",
                "Loan Risk Score",
                500,
                listOf(
                    factor("loan_bureau_score", listOf(range(0, 649, -120), range(650, 699, -20), range(700, 749, 40), range(750, 900, 90))),
                    factor("loan_avg_balance_inr", listOf(range(0, 24999, -40), range(25000, 74999, 25), range(75000, 999999999, 60))),
                    factor("loan_dti_ratio", listOf(range(0, 0.25, 60), range(0.251, 0.45, 15), range(0.451, 1.0, -80))),
                ),
            ),
            scorecard(
                "sc_sme_risk",
                "SME Risk Score",
                520,
                listOf(
                    factor("sme_gst_compliance_score", listOf(range(0, 79, -80), range(80, 89, 10), range(90, 100, 60))),
                    factor("sme_census_quality_score", listOf(range(0, 79, -60), range(80, 89, 10), range(90, 100, 45))),
                    factor("sme_loss_ratio_pct", listOf(range(0, 20, 50), range(20.1, 35.0, 10), range(35.1, 100.0, -70))),
                ),
            ),
        ),
        policies = listOf(
            Policy(
                id = "policy_travel_guard",
                name = "Travel Guard",
                steps = listOf(
                    PolicyStep(id = "travel_connector", type = "connector", refId = "travel", label = "Travel Profile"),
                    PolicyStep(
                        id = "travel_destination_callback",
                        type = "action",
                        label = "Destination Risk Lookup",
                        config = mapOf(
                            "url" to "rulemind://simulate/travel-callback",
                            "method" to "POST",
                            "bodyTemplate" to mapOf("destination_country" to "{{ payload.travel.destination_country }}"),
                            "onFailure" to "continue",
                        ),
                    ),
                    PolicyStep(id = "travel_passport_rule", type = "rule", refId = "rule_travel_passport_gate", label = "Passport Validity"),
                    PolicyStep(id = "travel_fit_score", type = "scorecard", refId = "sc_travel_fit", label = "Travel Fit Score"),
                    PolicyStep(id = "travel_destination_rule", type = "rule", refId = "rule_travel_destination_gate", label = "Destination Gate"),
                    PolicyStep(id = "travel_profile_rule", type = "rule", refId = "rule_travel_profile_gate", label = "Traveler Profile"),
                    PolicyStep(
                        id = "travel_review_gate",
                        type = "review_gate",
                        label = "Manual Travel Review",
                        config = mapOf(
                            "assignTo" to "travel_specialist_queue",
                            "requiredFields" to listOf("medical_clearance_note"),
                            "condition" to "outcome == 'review'",
                        ),
                    ),
                    PolicyStep(
                        id = "travel_outcome",
                        type = "outcome",
                        refId = "approve",
                        label = "Eligibility Confirmed",
                        config = mapOf("condition" to "outcome != 'reject' and outcome != 'review'"),
                    ),
                ),
                defaultOutcome = "review",
            ),
            Policy(
                id = "policy_instant_personal_loan",
                name = "Instant Personal Loan Underwriting",
                steps = listOf(
                    PolicyStep(id = "loan_connector", type = "connector", refId = "loan", label = "Loan Application"),
                    PolicyStep(id = "loan_bureau_rule", type = "rule", refId = "rule_loan_bureau_gate", label = "Bureau Threshold"),
                    PolicyStep(id = "loan_risk_score", type = "scorecard", refId = "sc_loan_risk", label = "Credit Risk Score"),
                    PolicyStep(
                        id = "loan_bureau_callback",
                        type = "action",
                        label = "Credit Bureau Callback",
                        config = mapOf(
                            "url" to "rulemind://simulate/loan-callback",
                            "method" to "POST",
                            "bodyTemplate" to mapOf("bureau_score" to "{{ variables.loan_bureau_score }}"),
                            "onFailure" to "continue",
                        ),
                    ),
                    PolicyStep(id = "loan_kyc_rule", type = "rule", refId = "rule_loan_kyc_gate", label = "KYC Gate"),
                    PolicyStep(id = "loan_geo_rule", type = "rule", refId = "rule_loan_geo_gate", label = "Geo Consistency"),
                    PolicyStep(id = "loan_affordability_rule", type = "rule", refId = "rule_loan_affordability_gate", label = "Affordability"),
                    PolicyStep(
                        id = "loan_review_gate",
                        type = "review_gate",
                        label = "Senior Underwriter Review",
                        config = mapOf(
                            "assignTo" to "underwriting_queue",
                            "requiredFields" to listOf("approved_amount_inr"),
                            "condition" to "outcome == 'review'",
                        ),
                    ),
                    PolicyStep(
                        id = "loan_outcome",
                        type = "outcome",
                        refId = "approve",
                        label = "Approved with Pricing",
                        config = mapOf("condition" to "outcome != 'reject' and outcome != 'review'"),
                    ),
                ),
                defaultOutcome = "review",
            ),
            Policy(
                id = "policy_sme_underwriting",
                name = "SME Underwriting",
                steps = listOf(
                    PolicyStep(id = "sme_connector", type = "connector", refId = "sme", label = "SME Profile"),
                    PolicyStep(
                        id = "sme_gst_callback",
                        type = "action",
                        label = "GST Verification Callback",
                        config = mapOf(
                            "url" to "rulemind://simulate/sme-callback",
                            "method" to "POST",
                            "bodyTemplate" to mapOf("gstin" to "{{ payload.sme.gstin }}"),
                            "onFailure" to "continue",
                        ),
                    ),
                    PolicyStep(id = "sme_compliance_rule", type = "rule", refId = "rule_sme_compliance_gate", label = "Compliance Gate"),
                    PolicyStep(id = "sme_census_rule", type = "rule", refId = "rule_sme_census_gate", label = "Census Quality"),
                    PolicyStep(id = "sme_risk_score", type = "scorecard", refId = "sc_sme_risk", label = "SME Risk Score"),
                    PolicyStep(id = "sme_claims_rule", type = "rule", refId = "rule_sme_claims_gate", label = "Claims Experience"),
                    PolicyStep(id = "sme_growth_rule", type = "rule", refId = "rule_sme_growth_gate", label = "Growth Routing"),
                    PolicyStep(
                        id = "sme_review_gate",
                        type = "review_gate",
                        label = "Committee Review",
                        config = mapOf(
                            "assignTo" to "regional_risk_committee",
                            "requiredFields" to listOf("underwriter_note"),
                            "condition" to "outcome == 'review'",
                        ),
                    ),
                    PolicyStep(
                        id = "sme_outcome",
                        type = "outcome",
                        refId = "approve",
                        label = "Instant Quote",
                        config = mapOf("condition" to "outcome != 'reject' and outcome != 'review'"),
                    ),
                    PolicyStep(
                        id = "sme_issuance_callback",
                        type = "action",
                        label = "Issuance Notice",
                        config = mapOf(
                            "url" to "rulemind://simulate/sme-issuance",
                            "method" to "POST",
                            "bodyTemplate" to mapOf(
                                "legal_company_name" to "{{ payload.sme.legal_company_name }}",
                                "score" to "{{ scorecard.sc_sme_risk.score }}",
                            ),
                            "onFailure" to "continue",
                            "condition" to "outcome == 'approve'",
                        ),
                    ),
                ),
                defaultOutcome = "review",
            ),
        ),
        experiments = emptyList<Experiment>(),
        serverOnlyVariables = emptyList(),
        checksum = "sha256:rulemind-mobile-studio",
    )

    private fun travelJourney() = StudioJourney(
        id = "travel_guard",
        title = "Travel Guard",
        category = "Travel Protection",
        policyId = "policy_travel_guard",
        stepCount = 7,
        primaryColor = 0xFF496FBEL,
        screens = listOf(
            StudioScreen("travel_landing", "Navigate Global Borders with Absolute Certainty.", "Automated trip eligibility and callback-aware verification.", "landing", bullets = listOf("Passport and visa readiness", "Destination risk overlays", "Coverage-fit assessment")),
            StudioScreen("trip_basics", "Trip & Traveler Basics", "Provide the essential details to tailor Travel Guard protection.", "form", fields = listOf(
                choice("destination_country", "Destination Country", listOf("Japan", "Portugal", "France", "Switzerland", "United Arab Emirates"), required = true),
                text("destination_city", "City / Region", required = true),
                number("trip_days", "Trip Duration (Days)", required = true, min = 1.0),
                choice("trip_purpose", "Trip Purpose", listOf("Leisure", "Business", "Education", "Adventure"), required = true),
                number("primary_traveller_age", "Primary Traveller Age", required = true, min = 18.0),
                choice("citizenship", "Citizenship", listOf("India", "United Kingdom", "Canada", "United States"), required = true),
            )),
            StudioScreen("travel_readiness", "Travel Readiness", "Ensure your documentation is in order before final eligibility assessment.", "form", fields = listOf(
                choice("passport_status", "Passport Availability", listOf("Currently in possession", "In renewal process", "Not yet applied"), required = true),
                number("passport_months_valid", "Passport Validity Remaining (Months)", required = true, min = 0.0),
                choice("visa_status", "Visa Status", listOf("Visa held or not required", "Applied / Pending approval", "Not started"), required = true),
                choice("medical_flag", "Medical Conditions", listOf("No material disclosures", "Additional coverage required"), required = true),
                choice("existing_insurance", "Insurance Purchased?", listOf("No", "Yes"), required = true),
            )),
            StudioScreen("coverage_preferences", "Coverage Preferences", "Customize your coverage for total peace of mind.", "form", fields = listOf(
                number("trip_cost_inr", "Total Trip Cost (INR)", required = true, min = 1000.0),
                choice("coverage_tier", "Coverage Tier", listOf("Standard Protection", "Platinum Shield"), required = true),
                choice("medical_add_on", "Emergency Medical", listOf("Included", "Excluded"), required = true),
                choice("baggage_add_on", "Baggage Protection", listOf("Included", "Excluded"), required = true),
            )),
            StudioScreen("travel_processing", "Verification & Callback Processing", "Aggregating traveler verification, destination support, and policy synthesis callbacks.", "processing"),
            StudioScreen("travel_decision", "Decision Outcome", "Premium eligibility, quote summary, rule log, and callback status.", "outcome"),
            StudioScreen("travel_audit", "Explainability & Audit View", "Decision rationale, callback history, threshold intersections, and action plan guidance.", "audit"),
        ),
    )

    private fun loanJourney() = StudioJourney(
        id = "instant_personal_loan",
        title = "Instant Personal Loan Underwriting",
        category = "Credit & Loans",
        policyId = "policy_instant_personal_loan",
        stepCount = 8,
        primaryColor = 0xFF32577FL,
        screens = listOf(
            StudioScreen("loan_landing", "Instant Personal Loan Underwriting.", "A high-fidelity simulation of automated credit assessment in India.", "landing", bullets = listOf("Bureau screening", "Affordability analysis", "KYC and fraud overlays")),
            StudioScreen("applicant_basics", "Applicant Basics", "Verify applicant details against regional registries and configure loan terms.", "form", fields = listOf(
                text("full_name", "Full Name", required = true),
                text("mobile_number", "Mobile Number", required = true, hint = "+91", keyboard = "phone"),
                text("email", "Email Address", required = true),
                text("pan", "PAN", required = true),
                choice("current_city", "Current City", listOf("Mumbai", "Bengaluru", "New Delhi", "Hyderabad", "Pune"), required = true),
                choice("employment_type", "Employment Type", listOf("Salaried", "Self-Employed"), required = true),
                number("requested_amount_inr", "Requested Loan Amount (INR)", required = true, min = 10000.0),
                choice("requested_tenure_months", "Requested Tenure", listOf("6", "24", "48", "60"), required = true),
            )),
            StudioScreen("income_employment", "Income & Employment Verification", "Assess credit stability and earning potential.", "form", fields = listOf(
                text("employer_name", "Employer Name", required = true),
                choice("employer_type", "Employer Type", listOf("Private Limited", "Public Sector", "Government", "Multinational"), required = true),
                number("monthly_income_inr", "Net Monthly Income (INR)", required = true, min = 10000.0),
                choice("salary_credit_mode", "Salary Credit Mode", listOf("Bank Transfer", "Cheque", "Cash"), required = true),
                number("work_experience_years", "Total Work Experience (Years)", required = true, min = 0.0),
                number("employer_tenure_months", "Current Employer Tenure (Months)", required = true, min = 0.0),
                number("salary_account_vintage_months", "Salary Account Vintage (Months)", required = true, min = 0.0),
            )),
            StudioScreen("obligations_stability", "Obligations & Stability", "Define the borrower's debt profile and recurring expenditure.", "form", fields = listOf(
                number("existing_emi_inr", "Current EMI Obligations (INR)", required = true, min = 0.0),
                number("active_loans", "Active Loans", required = true, min = 0.0),
                number("fixed_expenses_inr", "Monthly Fixed Expenses (INR)", required = true, min = 0.0),
                number("variable_expenses_inr", "Monthly Variable Expenses (INR)", required = true, min = 0.0),
                choice("residence_type", "Residence Type", listOf("Self-Owned", "Mortgaged / Financed", "Rented", "Parental / Shared"), required = true),
                number("housing_cost_inr", "Rent / Mortgage Amount (INR)", required = true, min = 0.0),
            )),
            StudioScreen("documents_identity", "Documents & Identity Inputs", "Identity, liveness, and income proof capture.", "form", fields = listOf(
                choice("income_proof_type", "Income Proof Type", listOf("Form 16 / ITR V", "Salary Certificate", "Certified P&L"), required = true),
                choice("utility_bill_verified", "Utility Bill Availability", listOf("Yes, Verified", "No / Alternative"), required = true),
                choice("pan_verified", "PAN Verification", listOf("Authenticated", "Mismatch"), required = true),
                number("liveness_score", "Liveness Match %", required = true, min = 0.0, max = 100.0),
                choice("geo_consistency", "Geo Consistency", listOf("Matched", "IP mismatch"), required = true),
            )),
            StudioScreen("loan_processing", "External Verification & Rule Processing", "Credit bureaus, fraud services, and bank statement parsers in motion.", "processing"),
            StudioScreen("loan_offer", "Offer Decision", "Risk band, approved amount, pricing, and underwriting rationale.", "outcome"),
            StudioScreen("loan_audit", "Underwriting Explainability & Audit", "Conditions, callback evidence, risk drivers, and workflow trace.", "audit"),
        ),
    )

    private fun smeJourney() = StudioJourney(
        id = "sme_underwriting",
        title = "SME Insurance, Compliance & Underwriting Workflow",
        category = "SME Business",
        policyId = "policy_sme_underwriting",
        stepCount = 10,
        primaryColor = 0xFF294860L,
        screens = listOf(
            StudioScreen("sme_landing", "SME Insurance, Compliance & Underwriting Workflow", "Automated risk triage from instant quote to committee review using India-ready business inputs.", "landing", bullets = listOf("KYB & sanctions", "Census quality", "Claims & compliance routing")),
            StudioScreen("business_onboarding", "Business Onboarding", "Core registration information as listed on official documents.", "form", fields = listOf(
                text("legal_company_name", "Legal Company Name", required = true),
                text("trade_name", "Trade Name"),
                choice("incorporation_type", "Incorporation Type", listOf("Private Limited", "Public Limited", "LLP", "Partnership", "Sole Proprietorship"), required = true),
                choice("industry_segment", "Industry Segment", listOf("Software & Technology", "Manufacturing", "Logistics", "Retail & FMCG", "Healthcare"), required = true),
                text("gstin", "GSTIN", required = true),
                text("company_pan", "Company PAN", required = true),
                number("years_in_business", "Years in Business", required = true, min = 0.0),
                number("employee_count", "Employee Count", required = true, min = 1.0),
                text("headquarters_city", "Headquarters City", required = true),
            )),
            StudioScreen("coverage_design", "Coverage Design", "Configure sum insured, policy structure, add-ons, and internal underwriting notes.", "form", fields = listOf(
                number("sum_insured_inr", "Sum Insured (INR)", required = true, min = 500000.0),
                choice("policy_type", "Policy Type", listOf("Family Floater", "Individual"), required = true),
                choice("dependent_inclusion", "Dependent Inclusion", listOf("Spouse", "Children", "Parents"), required = true, multi = true),
                choice("deductible", "Deductible", listOf("No Limit", "1% of Sum Insured", "Single Private Room"), required = true),
                choice("maternity_benefit", "Maternity Benefit", listOf("Enabled", "Disabled"), required = true),
                number("estimated_premium_inr", "Estimated Annual Premium (INR)", required = true, min = 10000.0),
            )),
            StudioScreen("employee_census", "Employee Census Intake", "Participant data collection, validation, and census-quality preview.", "form", fields = listOf(
                number("census_record_count", "Total Records", required = true, min = 1.0),
                number("census_validation_errors", "Validation Errors", required = true, min = 0.0),
                number("census_duplicate_hits", "Duplicate Hits", required = true, min = 0.0),
                number("census_quality_score", "Accuracy Score", required = true, min = 0.0, max = 100.0),
                number("median_age", "Median Employee Age", required = true, min = 18.0),
            )),
            StudioScreen("prior_policy_claims", "Prior Policy & Claims Experience", "Historical insurer, claims, exclusions, and loss-ratio profile.", "form", fields = listOf(
                choice("policy_type", "Policy Type", listOf("Renewal", "Fresh"), required = true),
                choice("existing_insurer", "Existing Insurer", listOf("ICICI Lombard", "AXA XL", "Allianz", "HDFC ERGO", "Chubb"), required = true),
                number("claims_count", "Claims Count", required = true, min = 0.0),
                number("loss_ratio_pct", "Loss Ratio %", required = true, min = 0.0),
                number("large_losses", "Large Losses", required = true, min = 0.0),
                choice("claims_disclosed", "Claims Fully Disclosed", listOf("Yes", "No"), required = true),
            )),
            StudioScreen("compliance_documents", "Compliance Documents", "Corporate identity, claims reports, census, and signed declaration readiness.", "form", fields = listOf(
                choice("coi_uploaded", "Certificate of Incorporation", listOf("Uploaded", "Missing"), required = true),
                choice("pan_uploaded", "Company PAN", listOf("Uploaded", "Missing"), required = true),
                choice("claims_report_uploaded", "Claims Report", listOf("Uploaded", "Missing"), required = true),
                choice("signed_declaration_uploaded", "Signed Declaration", listOf("Uploaded", "Missing"), required = true),
                choice("ubo_docs_ready", "UBO Documentation", listOf("Ready", "Pending"), required = true),
            )),
            StudioScreen("sme_processing", "External Verification & Parsing", "OCR, GST callback, anomaly detection, sanctions screening, and wait-state orchestration.", "processing"),
            StudioScreen("sme_routing", "Underwriting, Compliance & Routing Evaluation", "Support-guideline checks, fraud heuristics, and routing preview.", "processing"),
            StudioScreen("sme_outcome", "Final Outcome", "Instant quote, underwriter review, or compliance hold with conditions and blockers.", "outcome"),
            StudioScreen("sme_audit", "Full Explainability, Audit & Workflow Trace", "Decision blockers, callback evidence, actor trail, and remediation guidance.", "audit"),
        ),
    )

    private fun travelScenarios() = listOf(
        scenario("travel_happy_path", "travel_guard", "Happy Path Traveler", "Low Risk", "approve", "completed", mapOf("travel" to mapOf(
            "destination_country" to "Portugal", "destination_city" to "Lisbon", "trip_days" to 10, "trip_purpose" to "Leisure", "primary_traveller_age" to 34,
            "citizenship" to "India", "passport_status" to "Currently in possession", "passport_months_valid" to 14, "visa_status" to "Visa held or not required",
            "medical_flag" to "No material disclosures", "existing_insurance" to "No", "trip_cost_inr" to 145000, "coverage_tier" to "Standard Protection",
            "medical_add_on" to "Included", "baggage_add_on" to "Included", "destination_risk" to 1, "visa_ready" to 1, "support_matrix_ready" to 1, "medical_flag_value" to 0,
        ))),
        scenario("travel_senior_review", "travel_guard", "Senior Traveler Review", "Manual Review", "review", "paused", mapOf("travel" to mapOf(
            "destination_country" to "Japan", "destination_city" to "Osaka", "trip_days" to 14, "trip_purpose" to "Leisure", "primary_traveller_age" to 77,
            "citizenship" to "India", "passport_status" to "Currently in possession", "passport_months_valid" to 11, "visa_status" to "Visa held or not required",
            "medical_flag" to "Additional coverage required", "existing_insurance" to "Yes", "trip_cost_inr" to 220000, "coverage_tier" to "Platinum Shield",
            "medical_add_on" to "Included", "baggage_add_on" to "Included", "destination_risk" to 2, "visa_ready" to 1, "support_matrix_ready" to 1, "medical_flag_value" to 1,
        ))),
        scenario("travel_passport_failure", "travel_guard", "Passport Validity Failure", "Critical", "reject", "completed", mapOf("travel" to mapOf(
            "destination_country" to "France", "destination_city" to "Paris", "trip_days" to 7, "trip_purpose" to "Leisure", "primary_traveller_age" to 29,
            "citizenship" to "India", "passport_status" to "Currently in possession", "passport_months_valid" to 2, "visa_status" to "Visa held or not required",
            "medical_flag" to "No material disclosures", "existing_insurance" to "No", "trip_cost_inr" to 98000, "coverage_tier" to "Standard Protection",
            "medical_add_on" to "Included", "baggage_add_on" to "Included", "destination_risk" to 1, "visa_ready" to 1, "support_matrix_ready" to 1, "medical_flag_value" to 0,
        ))),
    )

    private fun loanScenarios() = listOf(
        scenario("loan_prime_approved", "instant_personal_loan", "Prime Borrower Approved", "Happy Path", "approve", "completed", mapOf("loan" to mapOf(
            "full_name" to "Aarav Mehta", "mobile_number" to "9876543210", "email" to "aarav.mehta@example.in", "pan" to "ABCDE1234F", "current_city" to "Mumbai",
            "employment_type" to "Salaried", "requested_amount_inr" to 850000, "requested_tenure_months" to 48, "employer_name" to "Axis Advisory Services",
            "employer_type" to "Multinational", "monthly_income_inr" to 185000, "salary_credit_mode" to "Bank Transfer", "work_experience_years" to 9,
            "employer_tenure_months" to 42, "salary_account_vintage_months" to 48, "existing_emi_inr" to 12000, "active_loans" to 1, "fixed_expenses_inr" to 18000,
            "variable_expenses_inr" to 22000, "residence_type" to "Self-Owned", "housing_cost_inr" to 0, "income_proof_type" to "Form 16 / ITR V",
            "utility_bill_verified" to "Yes, Verified", "pan_verified" to "Authenticated", "liveness_score" to 98.4, "geo_consistency" to "Matched",
            "bureau_score" to 756, "avg_balance_inr" to 124000, "dti_ratio" to 0.16, "pan_verified_flag" to 1, "geo_consistency_flag" to 0,
        ))),
        scenario("loan_borderline_review", "instant_personal_loan", "Borderline Borrower", "Review", "review", "paused", mapOf("loan" to mapOf(
            "full_name" to "Rhea Kapoor", "mobile_number" to "9898989898", "email" to "rhea.kapoor@example.in", "pan" to "PQRSK4567L", "current_city" to "Pune",
            "employment_type" to "Salaried", "requested_amount_inr" to 550000, "requested_tenure_months" to 60, "employer_name" to "Nextline Retail",
            "employer_type" to "Private Limited", "monthly_income_inr" to 72000, "salary_credit_mode" to "Bank Transfer", "work_experience_years" to 4,
            "employer_tenure_months" to 10, "salary_account_vintage_months" to 16, "existing_emi_inr" to 14000, "active_loans" to 2, "fixed_expenses_inr" to 18000,
            "variable_expenses_inr" to 13000, "residence_type" to "Rented", "housing_cost_inr" to 18000, "income_proof_type" to "Salary Certificate",
            "utility_bill_verified" to "Yes, Verified", "pan_verified" to "Authenticated", "liveness_score" to 95.0, "geo_consistency" to "Matched",
            "bureau_score" to 688, "avg_balance_inr" to 28000, "dti_ratio" to 0.44, "pan_verified_flag" to 1, "geo_consistency_flag" to 0,
        ))),
        scenario("loan_high_foir_reject", "instant_personal_loan", "High FOIR Reject", "Reject", "reject", "completed", mapOf("loan" to mapOf(
            "full_name" to "Kunal Sharma", "mobile_number" to "9123456780", "email" to "kunal.sharma@example.in", "pan" to "ZXCVB9876Q", "current_city" to "Hyderabad",
            "employment_type" to "Salaried", "requested_amount_inr" to 450000, "requested_tenure_months" to 36, "employer_name" to "Urban Fleet Labs",
            "employer_type" to "Private Limited", "monthly_income_inr" to 58000, "salary_credit_mode" to "Bank Transfer", "work_experience_years" to 3,
            "employer_tenure_months" to 9, "salary_account_vintage_months" to 12, "existing_emi_inr" to 24000, "active_loans" to 3, "fixed_expenses_inr" to 12000,
            "variable_expenses_inr" to 9000, "residence_type" to "Rented", "housing_cost_inr" to 16000, "income_proof_type" to "Form 16 / ITR V",
            "utility_bill_verified" to "Yes, Verified", "pan_verified" to "Authenticated", "liveness_score" to 97.0, "geo_consistency" to "Matched",
            "bureau_score" to 702, "avg_balance_inr" to 19000, "dti_ratio" to 0.62, "pan_verified_flag" to 1, "geo_consistency_flag" to 0,
        ))),
        scenario("loan_fraud_review", "instant_personal_loan", "Fraud Review Case", "Fraud Overlay", "review", "paused", mapOf("loan" to mapOf(
            "full_name" to "Meera Sethi", "mobile_number" to "9988776655", "email" to "meera.sethi@example.in", "pan" to "LMNOP4321K", "current_city" to "Bengaluru",
            "employment_type" to "Salaried", "requested_amount_inr" to 980000, "requested_tenure_months" to 48, "employer_name" to "Cloud Meridian",
            "employer_type" to "Multinational", "monthly_income_inr" to 210000, "salary_credit_mode" to "Bank Transfer", "work_experience_years" to 7,
            "employer_tenure_months" to 30, "salary_account_vintage_months" to 32, "existing_emi_inr" to 8000, "active_loans" to 1, "fixed_expenses_inr" to 15000,
            "variable_expenses_inr" to 18000, "residence_type" to "Self-Owned", "housing_cost_inr" to 0, "income_proof_type" to "Form 16 / ITR V",
            "utility_bill_verified" to "Yes, Verified", "pan_verified" to "Authenticated", "liveness_score" to 96.0, "geo_consistency" to "IP mismatch",
            "bureau_score" to 741, "avg_balance_inr" to 96000, "dti_ratio" to 0.18, "pan_verified_flag" to 1, "geo_consistency_flag" to 1,
        ))),
        scenario("loan_kyc_reject", "instant_personal_loan", "KYC Mismatch Reject", "KYC Mismatch", "reject", "completed", mapOf("loan" to mapOf(
            "full_name" to "Ishita Nair", "mobile_number" to "9811122233", "email" to "ishita.nair@example.in", "pan" to "AAAPA1122P", "current_city" to "Mumbai",
            "employment_type" to "Salaried", "requested_amount_inr" to 620000, "requested_tenure_months" to 24, "employer_name" to "Marina Commerce",
            "employer_type" to "Private Limited", "monthly_income_inr" to 98000, "salary_credit_mode" to "Bank Transfer", "work_experience_years" to 5,
            "employer_tenure_months" to 18, "salary_account_vintage_months" to 22, "existing_emi_inr" to 9000, "active_loans" to 1, "fixed_expenses_inr" to 14000,
            "variable_expenses_inr" to 16000, "residence_type" to "Rented", "housing_cost_inr" to 22000, "income_proof_type" to "Form 16 / ITR V",
            "utility_bill_verified" to "No / Alternative", "pan_verified" to "Mismatch", "liveness_score" to 82.0, "geo_consistency" to "Matched",
            "bureau_score" to 730, "avg_balance_inr" to 48000, "dti_ratio" to 0.24, "pan_verified_flag" to 0, "geo_consistency_flag" to 0,
        ))),
    )

    private fun smeScenarios() = listOf(
        scenario("sme_instant_quote", "sme_underwriting", "Clean SME Instant Quote", "Auto-rate", "approve", "completed", mapOf("sme" to mapOf(
            "legal_company_name" to "Apex Logistics Private Limited", "trade_name" to "Apex Logistics", "incorporation_type" to "Private Limited", "industry_segment" to "Logistics",
            "gstin" to "27AAACA1234F1Z5", "company_pan" to "AAACA1234F", "years_in_business" to 8, "employee_count" to 124, "headquarters_city" to "Mumbai",
            "sum_insured_inr" to 10000000, "policy_type" to "Family Floater", "dependent_inclusion" to listOf("Spouse", "Children"), "deductible" to "1% of Sum Insured",
            "maternity_benefit" to "Enabled", "estimated_premium_inr" to 142500, "census_record_count" to 1248, "census_validation_errors" to 4,
            "census_duplicate_hits" to 1, "census_quality_score" to 96.0, "median_age" to 34.2, "existing_insurer" to "ICICI Lombard", "claims_count" to 1,
            "loss_ratio_pct" to 14.5, "large_losses" to 0, "claims_disclosed" to "Yes", "coi_uploaded" to "Uploaded", "pan_uploaded" to "Uploaded",
            "claims_report_uploaded" to "Uploaded", "signed_declaration_uploaded" to "Uploaded", "ubo_docs_ready" to "Ready", "gst_compliance_score" to 94,
            "sanctions_flag" to 0, "growth_rate_pct" to 18, "ubo_docs_ready_flag" to 1, "claims_disclosed_flag" to 1,
        ))),
        scenario("sme_claims_review", "sme_underwriting", "High Claims Ratio Review", "Review", "review", "paused", mapOf("sme" to mapOf(
            "legal_company_name" to "Lumina Craft Brewery LLP", "trade_name" to "Lumina Craft Brewery", "incorporation_type" to "LLP", "industry_segment" to "Manufacturing",
            "gstin" to "29AAACL7788P1Z4", "company_pan" to "AAACL7788P", "years_in_business" to 5, "employee_count" to 86, "headquarters_city" to "Bengaluru",
            "sum_insured_inr" to 25000000, "policy_type" to "Individual", "dependent_inclusion" to listOf("Spouse"), "deductible" to "No Limit",
            "maternity_benefit" to "Disabled", "estimated_premium_inr" to 268000, "census_record_count" to 860, "census_validation_errors" to 18,
            "census_duplicate_hits" to 4, "census_quality_score" to 88.0, "median_age" to 37.0, "existing_insurer" to "AXA XL", "claims_count" to 12,
            "loss_ratio_pct" to 42.8, "large_losses" to 1, "claims_disclosed" to "Yes", "coi_uploaded" to "Uploaded", "pan_uploaded" to "Uploaded",
            "claims_report_uploaded" to "Uploaded", "signed_declaration_uploaded" to "Uploaded", "ubo_docs_ready" to "Ready", "gst_compliance_score" to 86,
            "sanctions_flag" to 0, "growth_rate_pct" to 72, "ubo_docs_ready_flag" to 1, "claims_disclosed_flag" to 1,
        ))),
        scenario("sme_census_failure", "sme_underwriting", "Census Quality Failure", "Clarification", "review", "paused", mapOf("sme" to mapOf(
            "legal_company_name" to "Northwind Health Services LLP", "trade_name" to "Northwind Health", "incorporation_type" to "LLP", "industry_segment" to "Healthcare",
            "gstin" to "07AAACN3322D1Z7", "company_pan" to "AAACN3322D", "years_in_business" to 6, "employee_count" to 220, "headquarters_city" to "New Delhi",
            "sum_insured_inr" to 18000000, "policy_type" to "Family Floater", "dependent_inclusion" to listOf("Spouse", "Children", "Parents"), "deductible" to "1% of Sum Insured",
            "maternity_benefit" to "Enabled", "estimated_premium_inr" to 312000, "census_record_count" to 1180, "census_validation_errors" to 114,
            "census_duplicate_hits" to 26, "census_quality_score" to 72.0, "median_age" to 38.5, "existing_insurer" to "HDFC ERGO", "claims_count" to 4,
            "loss_ratio_pct" to 26.0, "large_losses" to 0, "claims_disclosed" to "Yes", "coi_uploaded" to "Uploaded", "pan_uploaded" to "Uploaded",
            "claims_report_uploaded" to "Uploaded", "signed_declaration_uploaded" to "Uploaded", "ubo_docs_ready" to "Ready", "gst_compliance_score" to 90,
            "sanctions_flag" to 0, "growth_rate_pct" to 21, "ubo_docs_ready_flag" to 1, "claims_disclosed_flag" to 1,
        ))),
        scenario("sme_compliance_hold", "sme_underwriting", "Compliance Hold", "PEP Match Alert", "review", "paused", mapOf("sme" to mapOf(
            "legal_company_name" to "Global Tech Solutions Private Limited", "trade_name" to "Global Tech Solutions", "incorporation_type" to "Private Limited", "industry_segment" to "Software & Technology",
            "gstin" to "27AAACG5555Q1Z3", "company_pan" to "AAACG5555Q", "years_in_business" to 4, "employee_count" to 64, "headquarters_city" to "Pune",
            "sum_insured_inr" to 12500000, "policy_type" to "Individual", "dependent_inclusion" to listOf("Spouse"), "deductible" to "No Limit",
            "maternity_benefit" to "Enabled", "estimated_premium_inr" to 198000, "census_record_count" to 642, "census_validation_errors" to 8,
            "census_duplicate_hits" to 1, "census_quality_score" to 94.0, "median_age" to 31.8, "existing_insurer" to "ICICI Lombard", "claims_count" to 1,
            "loss_ratio_pct" to 12.0, "large_losses" to 0, "claims_disclosed" to "Yes", "coi_uploaded" to "Uploaded", "pan_uploaded" to "Uploaded",
            "claims_report_uploaded" to "Uploaded", "signed_declaration_uploaded" to "Uploaded", "ubo_docs_ready" to "Pending", "gst_compliance_score" to 91,
            "sanctions_flag" to 1, "growth_rate_pct" to 420, "ubo_docs_ready_flag" to 0, "claims_disclosed_flag" to 1,
        ))),
    )

    private fun scenario(
        id: String,
        journeyId: String,
        title: String,
        badge: String,
        expectedOutcome: String,
        expectedStatus: String,
        payload: Map<String, Any?>,
    ) = StudioScenario(id, journeyId, title, badge, expectedOutcome, expectedStatus, payload)

    private fun variable(id: String, sourceId: String, name: String, path: String) = CompiledVariable(
        id = id,
        sourceId = sourceId,
        name = name,
        returnType = "number",
        instructions = listOf(
            Instruction(op = "get", source = "payload", path = "$sourceId.$path", target = "result"),
            Instruction(op = "return", source = "result"),
        ),
    )

    private fun condition(variable: String, operator: String, value: Any?) = RuleTreeNode(
        type = "condition",
        variable = variable,
        operator = operator,
        value = value,
    )

    private fun rule(id: String, name: String, onPass: String, onFail: String, conditions: List<RuleTreeNode>) = CompiledRule(
        id = id,
        name = name,
        ruleFormat = "v2",
        tree = RuleTreeNode(
            type = "group",
            id = "${id}_root",
            logic = "AND",
            children = conditions,
            onPass = onPass,
            onFail = onFail,
        ),
    )

    private fun scorecard(id: String, name: String, baseScore: Int, bins: List<ScoreFactor>) = Scorecard(
        id = id,
        name = name,
        baseScore = baseScore,
        maxScore = 900,
        bins = bins,
    )

    private fun factor(variableId: String, ranges: List<ScoreRange>) = ScoreFactor(variableId = variableId, ranges = ranges)

    private fun range(min: Number, max: Number, points: Int) = ScoreRange(min = min.toDouble(), max = max.toDouble(), points = points)

    private fun entity(
        id: String,
        title: String,
        listPath: String,
        detailPath: String,
        createPath: String? = null,
        updatePath: String? = null,
        testPath: String? = null,
        draftTestPath: String? = null,
        supportsCreate: Boolean = false,
        supportsDelete: Boolean = false,
        supportsPromote: Boolean = false,
        fields: List<StudioField>,
    ) = StudioEntitySchema(id, title, listPath, detailPath, createPath, updatePath, testPath, draftTestPath, supportsCreate, supportsDelete, supportsPromote, fields)

    private fun text(id: String, label: String, required: Boolean = false, hint: String = "", keyboard: String = "text") =
        StudioField(id = id, label = label, kind = "text", required = required, hint = hint, keyboard = keyboard)

    private fun number(id: String, label: String, required: Boolean = false, min: Double? = null, max: Double? = null) =
        StudioField(id = id, label = label, kind = "number", required = required, keyboard = "number", min = min, max = max)

    private fun choice(id: String, label: String, options: List<String>, required: Boolean = false, multi: Boolean = false) =
        StudioField(id = id, label = label, kind = "choice", required = required, options = options, multi = multi)

    private fun json(id: String, label: String) = StudioField(id = id, label = label, kind = "json")

    private fun bool(id: String, label: String) = StudioField(id = id, label = label, kind = "boolean")

    private fun code(id: String, label: String, required: Boolean = false) = StudioField(id = id, label = label, kind = "code", required = required, language = "python")
}
