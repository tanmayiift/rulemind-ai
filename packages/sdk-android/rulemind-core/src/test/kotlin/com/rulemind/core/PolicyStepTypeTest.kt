package com.rulemind.core

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Policy
import com.rulemind.core.models.PolicyStep
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * The five step types the Python executor dispatches that the offline engine did not.
 *
 * `loop`, `branch` and `workflow` are pure control flow and now run on-device. `model` and
 * `monitor` need the server (a model blob + an ML runtime; audit events, alert webhooks and
 * scheduled re-evaluation) and now FAIL LOUDLY. And an unrecognised step type is an ERROR, as in
 * `executor.py`, not a silent `skipped` — a policy authored with a step this SDK version does not
 * understand must not quietly return a decision that step never contributed to.
 */
class PolicyStepTypeTest {
    private val executor = PolicyExecutor()

    private fun bundle(policies: List<Policy>) = Bundle(
        bundleVersion = 1,
        bundleId = "bundle-steps",
        tenantId = "tenant-1",
        compiledAt = "2026-09-03T00:00:00Z",
        expiresAt = "2026-10-03T00:00:00Z",
        variables = emptyList(),
        rules = emptyList(),
        scorecards = emptyList(),
        policies = policies,
        experiments = emptyList(),
        serverOnlyVariables = emptyList(),
        checksum = "checksum",
    )

    private fun run(steps: List<PolicyStep>, payload: Map<String, Any?> = emptyMap(), extra: List<Policy> = emptyList()) =
        Policy(id = "policy-root", name = "Root", steps = steps, defaultOutcome = "review").let { policy ->
            executor.evaluate(bundle(listOf(policy) + extra), policy, payload)
        }

    private fun traceFor(decision: com.rulemind.core.models.Decision, type: String): Map<String, Any?> =
        decision.trace.first { (it["step"] as Map<*, *>)["type"] == type }

    @Suppress("UNCHECKED_CAST")
    private fun resultFor(decision: com.rulemind.core.models.Decision, type: String): Map<String, Any?>? =
        traceFor(decision, type)["result"] as Map<String, Any?>?

    // ── loop ──

    @Test
    fun loopIteratesALiteralCollectionAndBindsItemAndIndex() {
        val decision = run(
            listOf(
                PolicyStep(
                    id = "loop-1", type = "loop",
                    config = mapOf(
                        "over" to listOf(10, 20, 30),
                        "steps" to listOf(
                            mapOf(
                                "id" to "approve-second", "type" to "outcome", "refId" to "approve",
                                "config" to mapOf("condition" to "index == 1"),
                            ),
                        ),
                    ),
                ),
            ),
        )
        val result = resultFor(decision, "loop")!!
        assertEquals(3, result["count"])
        assertEquals(3, result["iterations_run"])
        assertEquals(false, result["truncated"])
        assertEquals("sequential", result["mode"])
        // Only the second iteration's condition matched, so the outcome moved exactly once.
        assertEquals("approve", decision.outcome)
        @Suppress("UNCHECKED_CAST")
        val iterations = result["iterations"] as List<Map<String, Any?>>
        assertEquals(listOf(10, 20, 30), iterations.map { it["item"] })
        assertEquals("pending", iterations[0]["outcome_after"])
        assertEquals("approve", iterations[1]["outcome_after"])
    }

    @Test
    fun loopResolvesADottedPathAndAMapCollectionAndHonoursMaxIterations() {
        val fromPayload = run(
            listOf(PolicyStep(id = "loop-1", type = "loop", config = mapOf("over" to "payload.items", "steps" to emptyList<Any?>()))),
            payload = mapOf("items" to listOf(1, 2, 3, 4, 5)),
        )
        assertEquals(5, resultFor(fromPayload, "loop")!!["count"])

        val capped = run(
            listOf(
                PolicyStep(
                    id = "loop-1", type = "loop",
                    config = mapOf("over" to listOf(1, 2, 3, 4, 5), "maxIterations" to 2, "steps" to emptyList<Any?>()),
                ),
            ),
        )
        val cappedResult = resultFor(capped, "loop")!!
        assertEquals(5, cappedResult["count"])
        assertEquals(2, cappedResult["iterations_run"])
        assertEquals(true, cappedResult["truncated"])

        // A map iterates as {key, value} pairs; a non-iterable is a no-op, not an error.
        val overMap = run(
            listOf(PolicyStep(id = "loop-1", type = "loop", config = mapOf("over" to "payload.lookup", "steps" to emptyList<Any?>()))),
            payload = mapOf("lookup" to mapOf("a" to 1, "b" to 2)),
        )
        @Suppress("UNCHECKED_CAST")
        val pairs = resultFor(overMap, "loop")!!["iterations"] as List<Map<String, Any?>>
        assertEquals(mapOf("key" to "a", "value" to 1), pairs[0]["item"])

        val overNothing = run(
            listOf(PolicyStep(id = "loop-1", type = "loop", config = mapOf("over" to "payload.missing", "steps" to emptyList<Any?>()))),
        )
        assertEquals(0, resultFor(overNothing, "loop")!!["count"])
    }

    @Test
    fun nestingBeyondTheServersDepthLimitIsRefused() {
        // Ten nested loops is the Python executor's `_MAX_WORKFLOW_DEPTH`; the eleventh must not run.
        var innermost: Map<String, Any?> = mapOf("id" to "loop-11", "type" to "loop", "config" to mapOf("over" to listOf(1), "steps" to emptyList<Any?>()))
        repeat(10) { level ->
            innermost = mapOf(
                "id" to "loop-${10 - level}", "type" to "loop",
                "config" to mapOf("over" to listOf(1), "steps" to listOf(innermost)),
            )
        }
        val decision = run(
            listOf(
                PolicyStep(
                    id = "loop-0", type = "loop",
                    config = mapOf("over" to listOf(1), "steps" to listOf(innermost)),
                ),
            ),
        )
        val nestedErrors = decision.trace.mapNotNull { it["error"] as String? }
        assertTrue(
            nestedErrors.any { it.contains("nesting depth") },
            "expected a depth-limit error, got $nestedErrors",
        )
    }

    // ── branch ──

    @Test
    fun branchTakesTheFirstMatchThenTheDefault() {
        fun branchStep(payloadKey: String) = PolicyStep(
            id = "branch-1", type = "branch",
            config = mapOf(
                "branches" to listOf(
                    mapOf(
                        "condition" to "payload.$payloadKey == 'gold'", "label" to "gold",
                        "steps" to listOf(mapOf("id" to "s", "type" to "outcome", "refId" to "approve")),
                    ),
                    mapOf(
                        "condition" to "payload.$payloadKey == 'silver'", "label" to "silver",
                        "steps" to listOf(mapOf("id" to "s", "type" to "outcome", "refId" to "review")),
                    ),
                ),
                "default" to listOf(mapOf("id" to "s", "type" to "outcome", "refId" to "reject")),
            ),
        )

        val gold = run(listOf(branchStep("tier")), payload = mapOf("tier" to "gold"))
        assertEquals(0, resultFor(gold, "branch")!!["branch"])
        assertEquals("approve", gold.outcome)

        val silver = run(listOf(branchStep("tier")), payload = mapOf("tier" to "silver"))
        assertEquals(1, resultFor(silver, "branch")!!["branch"])
        assertEquals("review", silver.outcome)

        val other = run(listOf(branchStep("tier")), payload = mapOf("tier" to "bronze"))
        assertEquals("default", resultFor(other, "branch")!!["branch"])
        assertEquals("reject", other.outcome)
    }

    @Test
    fun branchWithNoMatchAndNoDefaultIsAnUnmatchedNoOp() {
        val decision = run(
            listOf(
                PolicyStep(
                    id = "branch-1", type = "branch",
                    config = mapOf(
                        "branches" to listOf(
                            mapOf("condition" to "payload.tier == 'gold'", "steps" to listOf(mapOf("id" to "s", "type" to "outcome", "refId" to "approve"))),
                        ),
                    ),
                ),
            ),
            payload = mapOf("tier" to "bronze"),
        )
        val result = resultFor(decision, "branch")!!
        assertEquals(false, result["matched"])
        assertNull(result["branch"])
        assertEquals("review", decision.outcome)
    }

    // ── workflow (sub-policy composition) ──

    @Test
    fun workflowRunsTheSubPolicyStepsAgainstTheLiveContext() {
        val sub = Policy(
            id = "policy-sub", name = "Sub",
            steps = listOf(PolicyStep(id = "sub-outcome", type = "outcome", refId = "approve")),
        )
        val decision = run(
            listOf(PolicyStep(id = "wf", type = "workflow", refId = "policy-sub")),
            extra = listOf(sub),
        )
        assertEquals("approve", decision.outcome)
        assertEquals(mapOf("workflow" to "policy-sub", "outcome" to "approve"), resultFor(decision, "workflow"))
    }

    @Test
    fun workflowRefusesCyclesAndUnknownReferences() {
        // The sub-policy calls the ROOT policy back; the root id seeds the visited set, so this is
        // caught on the first re-entry rather than recursing until the stack dies.
        val sub = Policy(
            id = "policy-sub", name = "Sub",
            steps = listOf(PolicyStep(id = "back", type = "workflow", refId = "policy-root")),
        )
        val cycle = run(listOf(PolicyStep(id = "wf", type = "workflow", refId = "policy-sub")), extra = listOf(sub))
        assertTrue(
            cycle.trace.any { (it["error"] as String?)?.contains("cycle detected") == true },
            "expected a cycle error, got ${cycle.trace.map { it["error"] }}",
        )

        val unknown = run(listOf(PolicyStep(id = "wf", type = "workflow", refId = "policy-nope")))
        assertEquals("Unknown sub-workflow: policy-nope", traceFor(unknown, "workflow")["error"])

        val missingRef = run(listOf(PolicyStep(id = "wf", type = "workflow")))
        assertEquals("Sub-workflow step missing ref_id", traceFor(missingRef, "workflow")["error"])
    }

    // ── model / monitor / unknown ──

    @Test
    fun modelAndMonitorFailExplicitlyInsteadOfBeingSkipped() {
        val model = run(listOf(PolicyStep(id = "m", type = "model", refId = "model_pd")))
        val modelError = traceFor(model, "model")["error"] as String?
        assertNotNull(modelError)
        assertTrue(modelError.contains("server runtime"), "unexpected message: $modelError")
        assertNull(resultFor(model, "model"), "a model step must not produce a result offline")

        val monitor = run(listOf(PolicyStep(id = "mon", type = "monitor")))
        val monitorError = traceFor(monitor, "monitor")["error"] as String?
        assertNotNull(monitorError)
        assertTrue(monitorError.contains("server-side telemetry"), "unexpected message: $monitorError")
    }

    @Test
    fun anUnknownStepTypeIsAnErrorNotASilentSkip() {
        val decision = run(listOf(PolicyStep(id = "x", type = "teleport")))
        val entry = traceFor(decision, "teleport")
        // Word-for-word with `executor.py`'s `"Unknown step type: {0}".format(step_type)`.
        assertEquals("Unknown step type: teleport", entry["error"])
        assertNull(entry["result"])
        // ...and it is NOT filed under serverOnlyStepsSkipped, which would read as "handled".
        assertEquals(emptyList(), decision.serverOnlyStepsSkipped)
    }

    @Test
    fun aStepThatAbortsStopsTheRunAndAThrowingGateEscalatesToReview() {
        val aborted = run(
            listOf(
                PolicyStep(id = "m", type = "model", refId = "model_pd", config = mapOf("onFailure" to "abort")),
                PolicyStep(id = "after", type = "outcome", refId = "approve"),
            ),
        )
        assertEquals("failed", aborted.status)
        assertEquals(1, aborted.trace.size)

        // A scorecard step whose reference is missing is a decision GATE that did not run; the
        // Python core escalates such a failure to human review rather than passing it over.
        val gate = run(listOf(PolicyStep(id = "sc", type = "scorecard", refId = "nope")))
        assertEquals("Unknown scorecard: nope", traceFor(gate, "scorecard")["error"])
        assertEquals("review", gate.outcome)
    }
}
