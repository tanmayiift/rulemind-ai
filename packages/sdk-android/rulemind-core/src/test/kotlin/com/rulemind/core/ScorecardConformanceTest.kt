package com.rulemind.core

import com.rulemind.core.models.ScoreFactor
import com.rulemind.core.models.ScoreRange
import com.rulemind.core.models.Scorecard
import com.rulemind.core.models.ScorecardMetric
import com.rulemind.core.models.WoeRange
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.math.abs
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Kotlin arm of the cross-engine SCORECARD conformance suite. Loads the shared fixture
 * (packages/shared/scorecards.spec.json), whose every `expected` block was produced by CALLING the
 * Python core (`app/logic.py evaluate_scorecard`), and asserts the offline engine reproduces the
 * score exactly for every case across all four scoring methods — plus the breakdown, the WoE
 * details, the information value and the derived metrics.
 *
 * Until this existed there was no shared scorecard corpus at all, which is why the offline engine
 * could implement points-only for so long without a single test noticing.
 */
class ScorecardConformanceTest {
    private val spec: JSONObject by lazy { JSONObject(locateSpec().readText()) }
    private val evaluator = ScorecardEvaluator()
    private val tolerance: Double by lazy { spec.getJSONObject("meta").optDouble("floatTolerance", 1e-9) }

    @Test
    fun everyCaseReproducesThePythonScore() {
        val cases = spec.getJSONArray("cases")
        assertTrue(cases.length() > 0, "the corpus must not be empty")
        var woeCases = 0
        var formulaCases = 0
        var metricCases = 0

        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val label = "case ${case.optString("name", i.toString())}"
            val scorecard = parseScorecard(case.getJSONObject("scorecard"))
            val variables = nativeMap(case.getJSONObject("variables"))
            val lookup = case.optJSONObject("variableLookup")?.let { raw ->
                raw.keys().asSequence().associateWith { nativeMap(raw.getJSONObject(it)) }
            } ?: emptyMap()
            val expected = case.getJSONObject("expected")

            val actual = evaluator.evaluate(scorecard, variables, lookup)

            assertEquals(expected.getInt("score"), actual["score"], "$label score")
            assertEquals(expected.getString("scoringMethod"), actual["scoring_method"], "$label scoring_method")
            assertEquals(expected.getInt("baseScore"), actual["base_score"], "$label base_score")
            assertEquals(expected.getInt("maxScore"), actual["max_score"], "$label max_score")

            @Suppress("UNCHECKED_CAST")
            val breakdown = actual["breakdown"] as List<Map<String, Any?>>
            assertEquals(expected.getInt("breakdownLength"), breakdown.size, "$label breakdown length")
            assertBreakdown(expected.getJSONArray("breakdown"), breakdown, label)

            when (scorecard.scoringMethod) {
                "woe" -> woeCases++
                "formula" -> formulaCases++
            }

            if (expected.getBoolean("hasWoeDetails")) {
                @Suppress("UNCHECKED_CAST")
                val details = actual["woe_details"] as? List<Map<String, Any?>>
                assertNotNull(details, "$label woe_details")
                assertNear(expected.getDouble("informationValue"), actual["information_value"], "$label information_value")
                assertWoeDetails(expected.getJSONArray("woeDetails"), details, label)
            } else {
                // Absent entirely when nothing matched — not present-and-zero.
                assertNull(actual["woe_details"], "$label woe_details must be absent")
                assertNull(actual["information_value"], "$label information_value must be absent")
            }

            if (expected.getBoolean("hasMetrics")) {
                metricCases++
                @Suppress("UNCHECKED_CAST")
                val metrics = actual["metrics"] as? Map<String, Map<String, Any?>>
                assertNotNull(metrics, "$label metrics")
                assertMetrics(expected.getJSONObject("metrics"), metrics, label)
            } else {
                assertNull(actual["metrics"], "$label metrics must be absent")
            }
        }

        // Guard against a corpus that quietly stops exercising the methods this test exists for.
        assertTrue(woeCases > 0, "corpus contains no WoE cases")
        assertTrue(formulaCases > 0, "corpus contains no formula cases")
        assertTrue(metricCases > 0, "corpus contains no metric cases")
    }

    // ── Assertions ──

    private fun assertBreakdown(expected: JSONArray, actual: List<Map<String, Any?>>, label: String) {
        for (i in 0 until expected.length()) {
            val want = expected.getJSONObject(i)
            val got = actual[i]
            val at = "$label breakdown[$i]"
            assertEquals(want.optStringOrNull("variableId"), got["variable_id"] as String?, "$at variable_id")
            assertEquals(want.optStringOrNull("variableName"), got["variable_name"] as String?, "$at variable_name")
            assertEquals(want.optStringOrNull("sourceId"), got["source_id"] as String?, "$at source_id")
            assertEquals(want.getInt("points").toLong(), (got["points"] as Int).toLong(), "$at points")
            assertEquals(want.getLong("weightedPoints"), got["weighted_points"], "$at weighted_points")
            assertNear(want.getDouble("weight"), got["weight"], "$at weight")
            assertNear(want.getDouble("coefficient"), got["coefficient"], "$at coefficient")
            assertNear(want.getDouble("woe"), got["woe"], "$at woe")
            assertSameValue(want.opt("value"), got["value"], "$at value")

            val wantRange = want.optJSONObject("activeRange")
            @Suppress("UNCHECKED_CAST")
            val gotRange = got["active_range"] as Map<String, Any?>?
            if (wantRange == null || want.isNull("activeRange")) {
                assertNull(gotRange, "$at active_range must be null")
            } else {
                assertNotNull(gotRange, "$at active_range")
                // The corpus echoes the RAW range dict, so a bound authored as the string "0"
                // stays a string there; the typed models parse bounds on the way in. Compare the
                // numbers, which is what the bin match actually turns on.
                assertSameBound(wantRange.opt("min"), gotRange["min"], "$at active_range.min")
                assertSameBound(wantRange.opt("max"), gotRange["max"], "$at active_range.max")
                // ...and `points` is likewise raw in the corpus (7.9), typed as int in the model,
                // matching Python's own `int(active_range.get("points", 0))` on the way to a score.
                assertSameBound(truncatedTowardZero(wantRange.opt("points")), gotRange["points"], "$at active_range.points")
            }
        }
    }

    private fun assertWoeDetails(expected: JSONArray, actual: List<Map<String, Any?>>, label: String) {
        assertEquals(expected.length(), actual.size, "$label woe_details length")
        for (i in 0 until expected.length()) {
            val want = expected.getJSONObject(i)
            val got = actual[i]
            val at = "$label woe_details[$i]"
            assertEquals(want.optStringOrNull("variable_id"), got["variable_id"] as String?, "$at variable_id")
            assertNear(want.getDouble("woe"), got["woe"], "$at woe")
            assertNear(want.getDouble("iv"), got["iv"], "$at iv")
            assertNear(want.getDouble("coefficient"), got["coefficient"], "$at coefficient")
            assertNear(want.getDouble("contribution"), got["contribution"], "$at contribution")
            assertSameValue(want.opt("event_rate"), got["event_rate"], "$at event_rate")
            assertSameValue(want.opt("non_event_rate"), got["non_event_rate"], "$at non_event_rate")
        }
    }

    private fun assertMetrics(expected: JSONObject, actual: Map<String, Map<String, Any?>>, label: String) {
        assertEquals(expected.keys().asSequence().toSet(), actual.keys, "$label metric names")
        for (name in expected.keys()) {
            val want = expected.getJSONObject(name)
            val got = actual.getValue(name)
            val at = "$label metrics[$name]"
            assertEquals(want.optString("unit", ""), got["unit"], "$at unit")
            assertEquals(want.optString("category", "financial"), got["category"], "$at category")
            val wantValue = want.opt("value")
            if (wantValue is JSONObject && wantValue.has("error")) {
                // The corpus records only THAT a metric failed — the real message embeds CPython's
                // SyntaxError text, which no other engine can reproduce. What must match is that
                // the metric produced an error object rather than a plausible number.
                val gotValue = got["value"]
                assertTrue(gotValue is Map<*, *> && gotValue.containsKey("error"), "$at should be an error, was $gotValue")
            } else {
                assertSameValue(wantValue, got["value"], "$at value")
            }
        }
    }

    // ── Comparison helpers ──

    private fun assertNear(expected: Double, actual: Any?, message: String) {
        val got = (actual as? Number)?.toDouble()
        assertNotNull(got, "$message: expected a number, got $actual")
        assertTrue(abs(expected - got) <= tolerance, "$message: expected $expected but was $got")
    }

    private fun truncatedTowardZero(raw: Any?): Any? {
        val number = (raw as? Number)?.toDouble() ?: (raw as? String)?.trim()?.toDoubleOrNull() ?: return raw
        return kotlin.math.truncate(number).toLong()
    }

    /** A bin bound: numeric after coercion, so `"0"` and `0.0` agree. */
    private fun assertSameBound(expected: Any?, actual: Any?, message: String) {
        val want = if (expected == JSONObject.NULL) null else expected
        if (want == null) {
            assertNull(actual, "$message: expected null but was $actual")
            return
        }
        val wantNumber = (want as? Number)?.toDouble() ?: (want as? String)?.trim()?.toDoubleOrNull()
        val gotNumber = (actual as? Number)?.toDouble() ?: (actual as? String)?.trim()?.toDoubleOrNull()
        assertNotNull(wantNumber, "$message: corpus bound $want is not numeric")
        assertNotNull(gotNumber, "$message: expected $want but was $actual")
        assertTrue(abs(wantNumber - gotNumber) <= tolerance, "$message: expected $want but was $actual")
    }

    /** Numbers compare within tolerance regardless of int/float shape; everything else exactly. */
    private fun assertSameValue(expected: Any?, actual: Any?, message: String) {
        val want = if (expected == JSONObject.NULL) null else expected
        if (want == null) {
            assertNull(actual, "$message: expected null but was $actual")
            return
        }
        if (want is Number && actual is Number) {
            assertTrue(
                abs(want.toDouble() - actual.toDouble()) <= tolerance,
                "$message: expected $want but was $actual",
            )
            return
        }
        assertEquals(want, actual, message)
    }

    // ── Corpus parsing (the Python/Kotlin/Dart scorecard shape: `bins` of factors) ──

    private fun parseScorecard(o: JSONObject): Scorecard = Scorecard(
        id = o.optString("id", "sc"),
        name = o.optString("name", "scorecard"),
        baseScore = o.optInt("base_score", 300),
        maxScore = o.optInt("max_score", 900),
        scoringMethod = o.optString("scoring_method", "points"),
        intercept = o.optDouble("intercept", 0.0),
        pdo = o.optDouble("pdo", 20.0),
        targetScore = o.optDouble("target_score", 600.0),
        targetOdds = o.optDouble("target_odds", 50.0),
        formula = o.optStringOrNull("formula"),
        metrics = o.optJSONArray("metrics").objects().map { metric ->
            ScorecardMetric(
                name = metric.optString("name", "unnamed"),
                formula = metric.optString("formula", "0"),
                unit = metric.optString("unit", ""),
                category = metric.optString("category", "financial"),
            )
        },
        bins = o.optJSONArray("bins").objects().map { factor ->
            ScoreFactor(
                variableId = factor.optStringOrNull("variable_id"),
                weight = factor.optDouble("weight", 1.0),
                coefficient = factor.optDouble("coefficient", 0.0),
                ranges = factor.optJSONArray("ranges").objects().map { range ->
                    val lower = range.bound("min")
                    val upper = range.bound("max")
                    ScoreRange(
                        min = lower.first, max = upper.first,
                        points = range.optInt("points", 0),
                        matchable = lower.second && upper.second,
                    )
                },
                woeValues = factor.optJSONArray("woe_values").objects().map { woe ->
                    val lower = woe.bound("min")
                    val upper = woe.bound("max")
                    WoeRange(
                        min = lower.first, max = upper.first,
                        woe = woe.optDouble("woe", 0.0),
                        iv = woe.optDouble("iv", 0.0),
                        eventRate = woe.optDouble("event_rate").takeUnless { it.isNaN() },
                        nonEventRate = woe.optDouble("non_event_rate").takeUnless { it.isNaN() },
                        matchable = lower.second && upper.second,
                    )
                },
            )
        },
    )

    /** (value, matchable): an ABSENT bound reads as 0.0; a null or non-numeric one skips the bin. */
    private fun JSONObject.bound(key: String): Pair<Double?, Boolean> {
        if (!has(key)) return null to true
        if (isNull(key)) return null to false
        val raw = optDouble(key)
        return if (raw.isNaN()) null to false else raw to true
    }

    private fun JSONObject.optStringOrNull(key: String): String? =
        if (has(key) && !isNull(key)) getString(key) else null

    private fun JSONArray?.objects(): List<JSONObject> =
        if (this == null) emptyList() else (0 until length()).mapNotNull { opt(it) as? JSONObject }

    private fun nativeMap(o: JSONObject): Map<String, Any?> =
        o.keys().asSequence().associateWith { native(o.get(it)) }

    private fun native(value: Any?): Any? = when (value) {
        JSONObject.NULL -> null
        is JSONObject -> nativeMap(value)
        is JSONArray -> (0 until value.length()).map { native(value.get(it)) }
        else -> value
    }

    private fun locateSpec(): File {
        val candidates = listOf(
            "../../shared/scorecards.spec.json",
            "../../../packages/shared/scorecards.spec.json",
            "packages/shared/scorecards.spec.json",
        )
        val base = File(System.getProperty("user.dir"))
        for (candidate in candidates) {
            val file = File(base, candidate)
            if (file.exists()) return file
        }
        error("Could not locate packages/shared/scorecards.spec.json from ${base.absolutePath}")
    }
}
