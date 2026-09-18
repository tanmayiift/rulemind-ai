package com.rulemind.android

import com.rulemind.core.models.Decision
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * `DecisionCodec` is what `ExecutionStore` writes and reads, so a round-trip defect here is a
 * resumability defect: an execution comes back subtly different from the one that was paused.
 * It had no tests until 2026-09-18.
 *
 * Robolectric rather than plain JUnit because `org.json` on a bare JVM is a stub that throws —
 * the Android implementation is what this code actually runs against.
 */
@RunWith(RobolectricTestRunner::class)
class DecisionCodecTest {

    private fun roundTrip(d: Decision): Decision =
        DecisionCodec.fromJson(JSONObject(DecisionCodec.toJson(d).toString()))

    @Test
    fun `a fully populated decision survives a round trip`() {
        val original = Decision(
            outcome = "review",
            score = 712.5,
            variables = mapOf("bureau_score" to 712, "dpd" to 0),
            ruleResults = listOf(mapOf("id" to "r1", "passed" to false)),
            executionId = "exec-9",
            status = "paused",
            pendingOperations = listOf(mapOf("status" to "queued", "kind" to "webhook")),
            policyId = "pol_loan",
            currentStepIndex = 3,
        )
        val back = roundTrip(original)

        assertEquals("review", back.outcome)
        assertEquals(712.5, back.score!!, 0.0001)
        assertEquals("exec-9", back.executionId)
        assertEquals("paused", back.status)
        assertEquals("pol_loan", back.policyId)
        assertEquals(3, back.currentStepIndex)
        assertEquals(1, back.ruleResults.size)
        assertEquals("queued", back.pendingOperations.first()["status"])
        assertEquals(712, (back.variables["bureau_score"] as Number).toInt())
    }

    @Test
    fun `an absent score stays null rather than becoming NaN or zero`() {
        // `optDouble` returns NaN for a missing key, and a score of 0.0 is a MEANINGFUL value in
        // a scorecard -- conflating "no score" with "scored zero" would silently change decisions.
        val back = roundTrip(Decision(outcome = "approve", score = null))
        assertNull("a missing score must stay null, not become 0.0 or NaN", back.score)
    }

    @Test
    fun `a score of zero survives as zero`() {
        val back = roundTrip(Decision(outcome = "reject", score = 0.0))
        assertEquals(0.0, back.score!!, 0.0001)
    }

    @Test
    fun `status defaults to completed when absent`() {
        assertEquals("completed", DecisionCodec.fromJson(JSONObject("""{"outcome":"approve"}""")).status)
    }

    @Test
    fun `blank optional strings decode as null rather than empty strings`() {
        // `optString` returns "" for a missing key. An executionId of "" would be stored under
        // the key `execution.` and could never be looked up again.
        val back = DecisionCodec.fromJson(JSONObject("""{"outcome":"approve"}"""))
        assertNull(back.executionId)
        assertNull(back.requestId)
        assertNull(back.policyId)
        assertNull(back.source)
    }

    @Test
    fun `an experiment object supplies id and variant when the flat fields are absent`() {
        val back = DecisionCodec.fromJson(
            JSONObject("""{"outcome":"approve","experiment":{"id":"exp1","variant":"challenger"}}"""))
        assertEquals("exp1", back.experimentId)
        assertEquals("challenger", back.experimentVariant)
    }

    @Test
    fun `an empty decision decodes without throwing`() {
        val back = DecisionCodec.fromJson(JSONObject("{}"))
        assertEquals("", back.outcome)
        assertTrue(back.variables.isEmpty())
        assertTrue(back.pendingOperations.isEmpty())
    }

    @Test
    fun `nested maps and lists keep their shape`() {
        val back = roundTrip(Decision(
            outcome = "approve",
            scorecardResults = mapOf("sc1" to mapOf("score" to 40, "band" to "A")),
            trace = listOf(mapOf("step" to "rule", "ok" to true)),
        ))
        assertEquals("A", back.scorecardResults["sc1"]?.get("band"))
        assertEquals(1, back.trace.size)
        assertEquals("rule", back.trace.first()["step"])
    }
}
