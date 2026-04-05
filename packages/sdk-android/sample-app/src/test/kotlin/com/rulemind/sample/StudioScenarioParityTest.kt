package com.rulemind.sample

import com.rulemind.core.RuleMindEngine
import com.rulemind.sample.viewmodel.StudioFixtures
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class StudioScenarioParityTest {
    private val engine = RuleMindEngine()
    private val content = StudioFixtures.demoContent()
    private val bundle = StudioFixtures.demoBundle()
    private val journeysById = content.journeys.associateBy { it.id }

    @Test
    fun seededScenariosMatchExpectedOutcomeAndStatus() {
        val observedStepTypes = linkedSetOf<String>()

        content.scenarios.forEach { scenario ->
            val journey = journeysById.getValue(scenario.journeyId)
            val decision = engine.evaluate(bundle, journey.policyId, scenario.payload, userId = "sample-app-test")

            assertEquals("Unexpected outcome for ${scenario.id}", scenario.expectedOutcome, decision.outcome)
            assertEquals("Unexpected status for ${scenario.id}", scenario.expectedStatus, decision.status)
            assertFalse("Trace should not be empty for ${scenario.id}", decision.trace.isEmpty())

            decision.trace.forEach { item ->
                val step = item["step"] as? Map<*, *>
                val stepType = step?.get("type")?.toString()
                if (!stepType.isNullOrBlank()) {
                    observedStepTypes += stepType
                }
            }

            if (scenario.expectedStatus == "paused") {
                assertNotNull("Paused scenarios must expose a review task for ${scenario.id}", decision.reviewTask)
            }

            if (scenario.id in setOf("travel_happy_path", "loan_prime_approved", "sme_instant_quote")) {
                assertTrue("Happy-path scenarios should queue at least one callback for ${scenario.id}", decision.pendingOperations.isNotEmpty())
            }
        }

        assertTrue("Rule coverage missing", "rule" in observedStepTypes)
        assertTrue("Scorecard coverage missing", "scorecard" in observedStepTypes)
        assertTrue("Action coverage missing", "action" in observedStepTypes)
        assertTrue("Review coverage missing", "review_gate" in observedStepTypes)
        assertTrue("Outcome coverage missing", "outcome" in observedStepTypes)
    }

    @Test
    fun pausedScenariosResumeToCompletedApprovals() {
        content.scenarios
            .filter { it.expectedStatus == "paused" }
            .forEach { scenario ->
                val journey = journeysById.getValue(scenario.journeyId)
                val paused = engine.evaluate(bundle, journey.policyId, scenario.payload, userId = "sample-app-test")
                val resumeIndex = maxOf(paused.currentStepIndex, (paused.pausedAtStep ?: 0) + 1)
                val resumed = engine.evaluate(
                    bundle,
                    journey.policyId,
                    scenario.payload,
                    userId = "sample-app-test",
                    resumeFrom = paused.copy(
                        status = "running",
                        currentStepIndex = resumeIndex,
                        pausedAtStep = null,
                        reviewTask = (paused.reviewTask ?: emptyMap()) + mapOf("status" to "approved"),
                        reviewResponse = mapOf(
                            "decision" to "approve",
                            "medical_clearance_note" to "QA approved",
                            "approved_amount_inr" to 50000,
                            "underwriter_note" to "QA approved",
                        ),
                    ),
                )

                assertEquals("Resumed review should complete for ${scenario.id}", "completed", resumed.status)
                assertEquals("Resumed review should approve for ${scenario.id}", "approve", resumed.outcome)
                assertTrue("Resumed trace should preserve or extend the workflow for ${scenario.id}", resumed.trace.size >= paused.trace.size)
            }
    }
}
