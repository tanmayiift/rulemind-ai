package com.rulemind.core

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.CompiledRule
import com.rulemind.core.models.CompiledVariable
import com.rulemind.core.models.Instruction
import com.rulemind.core.models.Policy
import com.rulemind.core.models.PolicyStep
import com.rulemind.core.models.ScoreFactor
import com.rulemind.core.models.ScoreRange
import com.rulemind.core.models.Scorecard
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class RuleMindEngineTest {
    @Test
    fun executesRulesScorecardsAndOutcomeWithExplainability() {
        val bundle = Bundle(
            bundleVersion = 1,
            bundleId = "bundle-1",
            tenantId = "tenant-1",
            compiledAt = "2026-03-26T00:00:00Z",
            expiresAt = "2026-04-26T00:00:00Z",
            variables = listOf(
                CompiledVariable(
                    id = "var_credit_score",
                    sourceId = "bureau",
                    name = "Credit Score",
                    instructions = listOf(
                        Instruction(op = "get", target = "score", path = "score"),
                        Instruction(op = "return", source = "score"),
                    ),
                ),
            ),
            rules = listOf(
                CompiledRule(
                    id = "rule_credit_check",
                    name = "Credit Check",
                    nodes = listOf(
                        mapOf(
                            "type" to "condition",
                            "variable" to "var_credit_score",
                            "operator" to ">=",
                            "value" to 700,
                        ),
                    ),
                ),
            ),
            scorecards = listOf(
                Scorecard(
                    id = "scorecard_credit_risk",
                    name = "Credit Risk",
                    baseScore = 300,
                    maxScore = 900,
                    bins = listOf(
                        ScoreFactor(
                            variableId = "var_credit_score",
                            ranges = listOf(ScoreRange(min = 700.0, max = 750.0, points = 275)),
                        ),
                    ),
                ),
            ),
            policies = listOf(
                Policy(
                    id = "policy-1",
                    name = "Policy",
                    steps = listOf(
                        PolicyStep(type = "rule", id = "rule-step", refId = "rule_credit_check", label = "Credit Eligibility"),
                        PolicyStep(type = "scorecard", id = "score-step", refId = "scorecard_credit_risk", label = "Credit Risk Scoring"),
                        PolicyStep(type = "outcome", id = "decision-step", label = "Final Decision", config = mapOf("outcome" to "approve")),
                    ),
                    defaultOutcome = "review",
                ),
            ),
            experiments = emptyList(),
            serverOnlyVariables = emptyList(),
            checksum = "sha256:test",
        )

        val decision = RuleMindEngine().evaluate(
            bundle,
            "policy-1",
            mapOf("bureau" to mapOf("score" to 720)),
        )

        assertEquals("approve", decision.outcome)
        assertEquals(575.0, decision.score)
        assertEquals("rule_credit_check", decision.ruleResults.first()["ruleId"])
        assertEquals(3, decision.trace.size)
        assertTrue(decision.serverOnlyStepsSkipped.isEmpty())
        assertEquals(3, decision.auditSummary["traceSteps"])
        assertEquals(275, ((decision.scorecardResults["scorecard_credit_risk"]?.get("breakdown") as List<*>).first() as Map<*, *>)["points"])
        assertNotNull(decision.explainability["rules"])
        assertNotNull(decision.explainability["scorecards"])
    }

    @Test
    fun queuesBlockingActionsAndSupportsPauseResumeFlows() {
        val bundle = Bundle(
            bundleVersion = 1,
            bundleId = "bundle-2",
            tenantId = "tenant-1",
            compiledAt = "2026-03-26T00:00:00Z",
            expiresAt = "2026-04-26T00:00:00Z",
            variables = emptyList(),
            rules = emptyList(),
            scorecards = emptyList(),
            policies = listOf(
                Policy(
                    id = "policy-workflow",
                    name = "Workflow Policy",
                    steps = listOf(
                        PolicyStep(
                            type = "action",
                            id = "callback-step",
                            label = "Fraud Callback",
                            config = mapOf(
                                "url" to "https://example.test/hooks",
                                "method" to "POST",
                                "bodyTemplate" to mapOf("applicationId" to "{{ payload.application_id }}"),
                                "onFailure" to "abort",
                            ),
                        ),
                        PolicyStep(
                            type = "review_gate",
                            id = "review-step",
                            label = "Manual Review",
                            config = mapOf("assignTo" to "ops_review"),
                        ),
                        PolicyStep(
                            type = "outcome",
                            id = "decision-step",
                            config = mapOf("outcome" to "approve"),
                        ),
                    ),
                    defaultOutcome = "review",
                ),
            ),
            experiments = emptyList(),
            serverOnlyVariables = emptyList(),
            checksum = "sha256:test",
        )

        val payload = mapOf("application_id" to "app-123")
        val firstPass = RuleMindEngine().evaluate(bundle, "policy-workflow", payload)
        assertEquals("pending_sync", firstPass.status)
        assertEquals(1, firstPass.pendingOperations.size)
        assertEquals(1, firstPass.actionResults.size)
        assertEquals(1, firstPass.currentStepIndex)

        val reviewPass = RuleMindEngine().evaluate(
            bundle,
            "policy-workflow",
            payload,
            resumeFrom = firstPass.copy(
                status = "running",
                pendingOperations = firstPass.pendingOperations.map { it + mapOf("status" to "delivered") },
                currentStepIndex = 1,
            ),
        )
        assertEquals("paused", reviewPass.status)
        assertEquals(1, reviewPass.pausedAtStep)
        assertEquals("review-step", reviewPass.reviewTask?.get("step_id"))

        val finalPass = RuleMindEngine().evaluate(
            bundle,
            "policy-workflow",
            payload,
            resumeFrom = reviewPass.copy(
                status = "running",
                pausedAtStep = null,
                currentStepIndex = 2,
                reviewTask = (reviewPass.reviewTask ?: emptyMap()) + mapOf("status" to "approved"),
                reviewResponse = mapOf("decision" to "approve"),
            ),
        )
        assertEquals("completed", finalPass.status)
        assertEquals("approve", finalPass.outcome)
        assertEquals(3, finalPass.trace.size)
    }
}
