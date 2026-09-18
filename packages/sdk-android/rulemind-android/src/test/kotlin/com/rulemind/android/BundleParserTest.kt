package com.rulemind.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * `BundleParser` turns the server's compiled bundle into the models the on-device engine runs.
 * 253 lines, and no tests until 2026-09-18.
 *
 * It is a PARITY surface, which is what makes it worth testing rather than merely covering: the
 * same policy must decide identically in Python, TypeScript, Rust, Kotlin and Dart, and a
 * parsing default that differs from the server's is a divergence the corpora cannot catch —
 * they compare evaluation, and this runs before evaluation begins.
 *
 * `bound()` is the sharpest example and most of this file is about it. It encodes a THREE-way
 * distinction the Python core makes and a naive reader would flatten to two.
 */
@RunWith(RobolectricTestRunner::class)
class BundleParserTest {

    private fun scorecardBundle(range: String): com.rulemind.core.models.Bundle =
        BundleParser.parseBundle(
            """{"bundleId":"b1","bundleVersion":3,"scorecards":[
                 {"id":"sc1","bins":[{"variableId":"v","ranges":[$range]}]}]}""")

    private fun firstRange(range: String) =
        scorecardBundle(range).scorecards.first().bins.first().ranges.first()

    @Test
    fun `top level fields parse`() {
        val b = BundleParser.parseBundle(
            """{"bundleId":"b1","bundleVersion":7,"tenantId":"t1","checksum":"abc"}""")
        assertEquals("b1", b.bundleId)
        assertEquals(7, b.bundleVersion)
        assertEquals("t1", b.tenantId)
        assertEquals("abc", b.checksum)
    }

    @Test
    fun `an empty bundle parses to empty collections rather than throwing`() {
        val b = BundleParser.parseBundle("{}")
        assertTrue(b.rules.isEmpty())
        assertTrue(b.scorecards.isEmpty())
        assertTrue(b.policies.isEmpty())
        assertTrue(b.variables.isEmpty())
    }

    // ---- bound(): absent / explicit null / non-numeric are THREE different things -------------

    @Test
    fun `an ABSENT bound is open-ended and still matchable`() {
        // Python reads a missing "min" as 0.0 and carries on, so the range still participates.
        val r = firstRange("""{"max":10,"points":5}""")
        assertNull("an absent min has no value", r.min)
        assertTrue("an absent bound must stay matchable", r.matchable)
    }

    @Test
    fun `an EXPLICIT NULL bound makes the range unmatchable`() {
        // `float(None)` raises TypeError in the Python core, and the `except ... continue` around
        // it means that candidate can never match. Treating null as "open-ended" here would make
        // the device match a range the server skips -- a silent cross-engine divergence.
        val r = firstRange("""{"min":null,"max":10,"points":5}""")
        assertNull(r.min)
        assertFalse("an explicit null must make the range unmatchable", r.matchable)
    }

    @Test
    fun `a NON-NUMERIC bound makes the range unmatchable`() {
        // `float("abc")` raises ValueError -- same branch, same consequence.
        val r = firstRange("""{"min":"abc","max":10,"points":5}""")
        assertNull(r.min)
        assertFalse("a non-numeric bound must make the range unmatchable", r.matchable)
    }

    @Test
    fun `an ordinary numeric range is matchable and keeps both bounds`() {
        val r = firstRange("""{"min":1,"max":10,"points":5}""")
        assertEquals(1.0, r.min!!, 0.0001)
        assertEquals(10.0, r.max!!, 0.0001)
        assertEquals(5, r.points)
        assertTrue(r.matchable)
    }

    @Test
    fun `one bad bound poisons the range even when the other is fine`() {
        val r = firstRange("""{"min":1,"max":null,"points":5}""")
        assertEquals(1.0, r.min!!, 0.0001)
        assertFalse("matchable is min AND max, not either", r.matchable)
    }

    // ---- camelCase / snake_case, which the server has emitted both of ------------------------

    @Test
    fun `scorecard fields accept both camelCase and snake_case`() {
        val camel = BundleParser.parseBundle(
            """{"scorecards":[{"id":"a","baseScore":100,"maxScore":800,"scoringMethod":"woe"}]}""")
            .scorecards.first()
        val snake = BundleParser.parseBundle(
            """{"scorecards":[{"id":"a","base_score":100,"max_score":800,"scoring_method":"woe"}]}""")
            .scorecards.first()
        assertEquals(camel.baseScore, snake.baseScore)
        assertEquals(camel.maxScore, snake.maxScore)
        assertEquals(camel.scoringMethod, snake.scoringMethod)
        assertEquals(100, snake.baseScore)
        assertEquals("woe", snake.scoringMethod)
    }

    @Test
    fun `scorecard defaults match the documented ones when fields are absent`() {
        val sc = BundleParser.parseBundle("""{"scorecards":[{"id":"a"}]}""").scorecards.first()
        assertEquals(300, sc.baseScore)
        assertEquals(900, sc.maxScore)
        assertEquals("points", sc.scoringMethod)
        assertEquals(20.0, sc.pdo, 0.0001)
        assertEquals(600.0, sc.targetScore, 0.0001)
        assertEquals(50.0, sc.targetOdds, 0.0001)
    }

    @Test
    fun `a blank scoringMethod falls back to points rather than staying empty`() {
        // `optString` yields "" for a present-but-empty field, and an empty method would not
        // match any branch in the engine -- the scorecard would silently score nothing.
        val sc = BundleParser.parseBundle(
            """{"scorecards":[{"id":"a","scoringMethod":""}]}""").scorecards.first()
        assertEquals("points", sc.scoringMethod)
    }

    @Test
    fun `woe ranges honour the same bound rules as score ranges`() {
        val woe = BundleParser.parseBundle(
            """{"scorecards":[{"id":"a","bins":[{"variableId":"v","woe_values":[
                 {"min":null,"max":5,"woe":0.3}]}]}]}""")
            .scorecards.first().bins.first().woeValues.first()
        assertFalse("woe ranges use bound() too", woe.matchable)
        assertEquals(0.3, woe.woe, 0.0001)
    }
}
