package com.rulemind.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Kotlin arm of the cross-engine operator conformance suite. Loads the same
 * fixture file as the TS, Python, and Dart engines
 * (packages/shared/operators.spec.json) and asserts the offline engine's
 * operator semantics match exactly.
 */
class OperatorConformanceTest {
    private val spec: JSONObject by lazy { JSONObject(locateSpec().readText()) }
    private val evaluator = RuleEvaluator()

    @Test
    fun everyOperatorHasFixture() {
        val cases = spec.getJSONArray("cases")
        val covered = HashSet<String>()
        for (i in 0 until cases.length()) {
            covered.add(cases.getJSONObject(i).getString("operator"))
        }
        val operators = spec.getJSONArray("operators")
        for (i in 0 until operators.length()) {
            val op = operators.getJSONObject(i).getString("op")
            assertTrue(covered.contains(op), "no fixture for operator $op")
        }
    }

    @Test
    fun everyFixtureCaseMatches() {
        val cases: JSONArray = spec.getJSONArray("cases")
        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val name = case.getString("name")
            val result = evaluator.compareCondition(
                jsonValue(case, "actual"),
                case.getString("operator"),
                jsonValue(case, "value"),
                jsonValue(case, "value2"),
                if (case.isNull("fieldType")) null else case.optString("fieldType"),
            )
            assertEquals(case.getBoolean("expected"), result, "$name: ${case.getString("operator")}")
        }
    }

    /** Convert a JSON field into the native Kotlin type the engine expects. */
    private fun jsonValue(obj: JSONObject, key: String): Any? {
        if (!obj.has(key) || obj.isNull(key)) return null
        return when (val raw = obj.get(key)) {
            is JSONArray -> (0 until raw.length()).map { raw.get(it) }
            else -> raw
        }
    }

    private fun locateSpec(): File {
        val candidates = listOf(
            "../../shared/operators.spec.json",
            "../../../packages/shared/operators.spec.json",
            "packages/shared/operators.spec.json",
        )
        val base = File(System.getProperty("user.dir"))
        for (candidate in candidates) {
            val file = File(base, candidate)
            if (file.exists()) return file
        }
        error("Could not locate packages/shared/operators.spec.json from ${base.absolutePath}")
    }
}
