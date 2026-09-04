package com.rulemind.core

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Policy
import com.rulemind.core.models.PolicyStep
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

/**
 * A polling action queued on device must hand the host everything it needs to poll.
 *
 * This engine does no outbound I/O, so an action step becomes a pending operation that the host
 * app performs. `mode: "poll"` is not a semantics this engine implements -- it is a semantics it
 * must not LOSE. The server executor calls the endpoint until the author's condition holds and
 * fails CLOSED (review) if it never does; a host handed only url/method/body fires once and
 * reports whatever came back, which for a job-status endpoint is `{"status": "pending"}` treated
 * as a final answer. Same policy, same input, opposite decision -- on the two runtimes the parity
 * contract exists to keep in agreement.
 */
class ActionPollingHandoffTest {

    private fun bundleWith(config: Map<String, Any?>) = Bundle(
        bundleVersion = 1,
        bundleId = "bundle-poll",
        tenantId = "tenant-1",
        compiledAt = "2026-03-26T00:00:00Z",
        expiresAt = "2026-04-26T00:00:00Z",
        variables = emptyList(),
        rules = emptyList(),
        scorecards = emptyList(),
        policies = listOf(
            Policy(
                id = "policy-poll",
                name = "Polling Policy",
                steps = listOf(
                    PolicyStep(type = "action", id = "kyc-status", label = "KYC status", config = config),
                    PolicyStep(type = "outcome", id = "decision-step", config = mapOf("outcome" to "approve")),
                ),
                defaultOutcome = "review",
            ),
        ),
        experiments = emptyList(),
        serverOnlyVariables = emptyList(),
        checksum = "sha256:test",
    )

    private val pollConfig = mapOf(
        "url" to "https://api.example.test/jobs/1",
        "method" to "GET",
        "mode" to "poll",
        "pollUntil" to mapOf("path" to "body.status", "operator" to "==", "value" to "complete"),
        "maxPolls" to 5,
        "pollIntervalMs" to 1000,
    )

    @Test
    fun aQueuedPollCarriesItsConditionAndBounds() {
        val result = RuleMindEngine().evaluate(bundleWith(pollConfig), "policy-poll", emptyMap())
        assertEquals(1, result.pendingOperations.size)
        val op = result.pendingOperations.first()

        assertEquals("poll", op["mode"], "the host cannot tell this from a fire-and-forget action")
        assertNotNull(op["pollUntil"], "the exit condition was dropped; the host has nothing to poll until")
        assertEquals(5, op["maxPolls"])
        assertEquals(1000, op["pollIntervalMs"])
        // Fail closed, and it must survive the handoff -- a host defaulting to "continue" would
        // let a never-finished job through as though it had finished.
        assertEquals("review_gate", op["onPollTimeout"])
        // The ordinary fields are still there: poll config is additive, not a replacement.
        assertEquals("https://api.example.test/jobs/1", op["url"])
        assertEquals("GET", op["method"])
    }

    @Test
    fun anOrdinaryActionIsUnchanged() {
        val result = RuleMindEngine().evaluate(
            bundleWith(mapOf("url" to "https://api.example.test/hooks", "method" to "POST")),
            "policy-poll", emptyMap(),
        )
        val op = result.pendingOperations.first()
        assertFalse(op.containsKey("mode"), "a non-poll action was tagged as a poll")
        assertFalse(op.containsKey("pollUntil"))
        assertFalse(op.containsKey("onPollTimeout"))
        assertEquals("https://api.example.test/hooks", op["url"])
    }

    @Test
    fun theExplicitTimeoutChoiceIsPreservedRatherThanForcedToTheDefault() {
        val result = RuleMindEngine().evaluate(
            bundleWith(pollConfig + mapOf("onPollTimeout" to "abort")), "policy-poll", emptyMap(),
        )
        assertEquals("abort", result.pendingOperations.first()["onPollTimeout"])
    }

    @Test
    fun pollingDoesNotChangeWhetherTheStepBlocks() {
        // `blocking` is derived from onFailure == "abort" and nothing else. Polling must not
        // silently make a non-blocking action block, which would strand the execution.
        val result = RuleMindEngine().evaluate(bundleWith(pollConfig), "policy-poll", emptyMap())
        assertTrue(result.pendingOperations.first()["blocking"] == false)
    }
}
