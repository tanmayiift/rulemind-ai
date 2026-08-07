package com.rulemind.core

import com.rulemind.core.models.CompiledRule
import com.rulemind.core.models.RuleTreeNode
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Kotlin arm of the cross-engine LARGE-policy conformance suite. Loads the shared
 * fixture (packages/shared/large-policy.spec.json) — one v2 rule of 600 conditions
 * across 750 variables (all 12 operators, mixed AND/OR/NOT) — and asserts the offline
 * engine reproduces BOTH the Python core's outcome AND its number of passed conditions
 * for every case, including payloads that omit variables (missing-variable parity).
 */
class LargePolicyConformanceTest {
    private val spec: JSONObject by lazy { JSONObject(locateSpec().readText()) }
    private val evaluator = RuleEvaluator()

    @Test
    fun everyCaseMatchesOutcomeAndPassedCount() {
        val rule = CompiledRule(
            id = spec.getJSONObject("rule").getString("id"),
            name = "Large policy",
            ruleFormat = "v2",
            tree = parseTree(spec.getJSONObject("rule").getJSONObject("tree")),
        )
        val cases = spec.getJSONArray("cases")
        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            @Suppress("UNCHECKED_CAST")
            val variables = native(case.getJSONObject("variables")) as Map<String, Any?>
            val result = evaluator.evaluate(rule, variables)
            assertEquals(case.getString("expectedOutcome"), result["outcome"], "case $i outcome")
            @Suppress("UNCHECKED_CAST")
            val passed = (result["conditions"] as List<Map<String, Any?>>).count { it["passed"] == true }
            assertEquals(case.getInt("trueConditions"), passed, "case $i passed-count")
        }
    }

    /**
     * Negative/boundary coverage: the flat-AND threshold rule (onFail=reject) must reproduce the
     * exact passed-count AND fail (reject) for sub-threshold inputs — 500/500 true approves, 499/500
     * rejects. The big OR-rule above can't express a clean count threshold, so this closes the gap.
     */
    @Test
    fun thresholdRuleBoundaryAndNegatives() {
        val rule = CompiledRule(
            id = spec.getJSONObject("thresholdRule").getString("id"),
            name = "Threshold policy",
            ruleFormat = "v2",
            tree = parseTree(spec.getJSONObject("thresholdRule").getJSONObject("tree")),
        )
        val cases = spec.getJSONArray("thresholdCases")
        val byTarget = HashMap<Int, String?>()
        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            @Suppress("UNCHECKED_CAST")
            val variables = native(case.getJSONObject("variables")) as Map<String, Any?>
            val result = evaluator.evaluate(rule, variables)
            assertEquals(case.getString("expectedOutcome"), result["outcome"], "threshold case $i outcome")
            @Suppress("UNCHECKED_CAST")
            val passed = (result["conditions"] as List<Map<String, Any?>>).count { it["passed"] == true }
            assertEquals(case.getInt("trueConditions"), passed, "threshold case $i passed-count")
            byTarget[case.getInt("targetTrue")] = result["outcome"] as String?
        }
        val n = spec.getJSONObject("meta").getInt("thresholdConditions")
        assertEquals("approve", byTarget[n], "all-true must approve")
        assertEquals("reject", byTarget[n - 1], "one-below-threshold MUST reject")
        assertEquals("reject", byTarget[0], "none-true must reject")
    }

    private fun parseTree(o: JSONObject): RuleTreeNode = RuleTreeNode(
        type = o.getString("type"),
        id = o.optStringOrNull("id"),
        variable = o.optStringOrNull("variable"),
        operator = o.optStringOrNull("operator"),
        value = jsonScalar(o, "value"),
        value2 = jsonScalar(o, "value2"),
        fieldType = o.optStringOrNull("fieldType"),
        logic = o.optStringOrNull("logic"),
        children = if (o.has("children")) o.getJSONArray("children").let { arr ->
            (0 until arr.length()).map { parseTree(arr.getJSONObject(it)) }
        } else emptyList(),
        child = if (o.has("child") && !o.isNull("child")) parseTree(o.getJSONObject("child")) else null,
        onPass = o.optStringOrNull("onPass"),
        onFail = o.optStringOrNull("onFail"),
    )

    private fun JSONObject.optStringOrNull(key: String): String? =
        if (has(key) && !isNull(key)) getString(key) else null

    private fun jsonScalar(o: JSONObject, key: String): Any? =
        if (!o.has(key) || o.isNull(key)) null else o.get(key)

    private fun native(value: Any?): Any? = when (value) {
        is JSONObject -> value.keys().asSequence().associateWith { native(value.get(it)) }
        is JSONArray -> (0 until value.length()).map { native(value.get(it)) }
        JSONObject.NULL -> null
        else -> value
    }

    private fun locateSpec(): File {
        val candidates = listOf(
            "../../shared/large-policy.spec.json",
            "../../../packages/shared/large-policy.spec.json",
            "packages/shared/large-policy.spec.json",
        )
        val base = File(System.getProperty("user.dir"))
        for (candidate in candidates) {
            val file = File(base, candidate)
            if (file.exists()) return file
        }
        error("Could not locate packages/shared/large-policy.spec.json from ${base.absolutePath}")
    }
}
