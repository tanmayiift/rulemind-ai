package com.rulemind.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Kotlin arm of the cross-engine DECISION-TABLE conformance suite. Loads the same
 * fixture as the Python and Dart engines (packages/shared/decision-tables.spec.json)
 * and asserts the offline evaluator resolves the SAME outcome as the Python core
 * (the source of truth) for every case.
 */
class DecisionTableConformanceTest {
    private val spec: JSONObject by lazy { JSONObject(locateSpec().readText()) }
    private val evaluator = DecisionTableEvaluator()

    @Test
    fun everyCaseMatchesExpectedOutcome() {
        val tables = spec.getJSONObject("tables")
        val cases = spec.getJSONArray("cases")
        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            @Suppress("UNCHECKED_CAST")
            val table = native(tables.getJSONObject(case.getString("table"))) as Map<String, Any?>
            @Suppress("UNCHECKED_CAST")
            val variables = native(case.getJSONObject("variables")) as Map<String, Any?>
            val result = evaluator.evaluate(table, variables)
            assertEquals(case.getString("expectedOutcome"), result["outcome"], case.getString("name"))
        }
    }

    /** Recursively convert JSON into native Kotlin Map/List/scalar the evaluator reads. */
    private fun native(value: Any?): Any? = when (value) {
        is JSONObject -> value.keys().asSequence().associateWith { native(value.get(it)) }
        is JSONArray -> (0 until value.length()).map { native(value.get(it)) }
        JSONObject.NULL -> null
        else -> value
    }

    private fun locateSpec(): File {
        val candidates = listOf(
            "../../shared/decision-tables.spec.json",
            "../../../packages/shared/decision-tables.spec.json",
            "packages/shared/decision-tables.spec.json",
        )
        val base = File(System.getProperty("user.dir"))
        for (candidate in candidates) {
            val file = File(base, candidate)
            if (file.exists()) return file
        }
        error("Could not locate packages/shared/decision-tables.spec.json from ${base.absolutePath}")
    }
}
