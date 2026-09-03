package com.rulemind.android

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.CompiledRule
import com.rulemind.core.models.CompiledVariable
import com.rulemind.core.models.Experiment
import com.rulemind.core.models.ExperimentVariant
import com.rulemind.core.models.Instruction
import com.rulemind.core.models.Policy
import com.rulemind.core.models.PolicyStep
import com.rulemind.core.models.RuleTreeNode
import com.rulemind.core.models.ScoreFactor
import com.rulemind.core.models.ScoreRange
import com.rulemind.core.models.Scorecard
import com.rulemind.core.models.ScorecardMetric
import com.rulemind.core.models.WoeRange
import org.json.JSONArray
import org.json.JSONObject

internal object BundleParser {
    fun parseBundle(json: String): Bundle {
        val root = JSONObject(json)
        return Bundle(
            bundleVersion = root.optInt("bundleVersion"),
            bundleId = root.optString("bundleId"),
            tenantId = root.optString("tenantId"),
            compiledAt = root.optString("compiledAt"),
            expiresAt = root.optString("expiresAt"),
            variables = root.optJSONArray("variables").toInstructionVariables(),
            rules = root.optJSONArray("rules").toRules(),
            scorecards = root.optJSONArray("scorecards").toScorecards(),
            policies = root.optJSONArray("policies").toPolicies(),
            experiments = root.optJSONArray("experiments").toExperiments(),
            serverOnlyVariables = root.optJSONArray("serverOnlyVariables").toStringList(),
            checksum = root.optString("checksum"),
        )
    }

    private fun JSONArray?.toInstructionVariables(): List<CompiledVariable> = jsonObjects(this).map { item ->
        CompiledVariable(
            id = item.optString("id"),
            sourceId = item.optString("sourceId", item.optString("source_id")),
            name = item.optString("name"),
            returnType = item.optString("returnType", item.optString("return_type")).ifBlank { null },
            instructions = item.optJSONArray("instructions").toInstructions(),
            compilable = item.optBoolean("compilable", true),
        )
    }

    private fun JSONArray?.toInstructions(): List<Instruction> = jsonObjects(this).map { item ->
        Instruction(
            op = item.optString("op"),
            target = item.optString("target").ifBlank { null },
            source = item.optString("source").ifBlank { null },
            sources = item.optJSONArray("sources").toStringList(),
            left = item.optString("left").ifBlank { null },
            right = item.optString("right").ifBlank { null },
            args = item.optJSONArray("args").toStringList(),
            path = item.optString("path").ifBlank { null },
            key = item.optString("key").ifBlank { null },
            // The compiler emits "default"; "defaultValue" is only the legacy spelling.
            defaultValue = (item.opt("default") ?: item.opt("defaultValue")).takeUnless { it == JSONObject.NULL },
            type = item.optString("type").ifBlank { null },
            value = item.opt("value").takeUnless { it == JSONObject.NULL },
            values = item.optJSONArray("values").toAnyList(),
            predicatePath = item.optString("predicatePath", item.optString("predicate_path")).ifBlank { null },
            predicateOperator = item.optString("predicateOperator", item.optString("predicate_operator")).ifBlank { null },
            predicateValue = item.opt("predicateValue").takeUnless { it == JSONObject.NULL },
            thenSource = item.optString("thenSource").ifBlank { null },
            elseSource = item.optString("elseSource").ifBlank { null },
            thenValue = item.opt("thenValue").takeUnless { it == JSONObject.NULL },
            elseValue = item.opt("elseValue").takeUnless { it == JSONObject.NULL },
        )
    }

    private fun JSONArray?.toRules(): List<CompiledRule> = jsonObjects(this).map { item ->
        CompiledRule(
            id = item.optString("id"),
            name = item.optString("name"),
            ruleFormat = item.optString("ruleFormat", item.optString("rule_format", "v1")),
            tree = item.optJSONObject("tree")?.toRuleTree(),
            nodes = item.optJSONArray("nodes").toMaps(),
            expression = item.optString("expression").ifBlank { null },
        )
    }

    private fun JSONObject.toRuleTree(): RuleTreeNode = RuleTreeNode(
        type = optString("type"),
        id = optString("id").ifBlank { null },
        variable = optString("variable").ifBlank { null },
        operator = optString("operator").ifBlank { null },
        value = opt("value").takeUnless { it == JSONObject.NULL },
        logic = optString("logic").ifBlank { null },
        children = optJSONArray("children").let { array -> jsonObjects(array).map { child -> child.toRuleTree() } },
        child = optJSONObject("child")?.toRuleTree(),
        onPass = optString("onPass", optString("on_pass")).ifBlank { null },
        onFail = optString("onFail", optString("on_fail")).ifBlank { null },
    )

    private fun JSONArray?.toScorecards(): List<Scorecard> = jsonObjects(this).map { item ->
        Scorecard(
            id = item.optString("id"),
            name = item.optString("name"),
            baseScore = item.optInt("baseScore", item.optInt("base_score", 300)),
            maxScore = item.optInt("maxScore", item.optInt("max_score", 900)),
            scoringMethod = item.optString("scoringMethod", item.optString("scoring_method", "points")).ifBlank { "points" },
            intercept = item.optDouble("intercept", 0.0).orElse(0.0),
            pdo = item.optDouble("pdo", 20.0).orElse(20.0),
            targetScore = item.optDouble("targetScore", item.optDouble("target_score", 600.0)).orElse(600.0),
            targetOdds = item.optDouble("targetOdds", item.optDouble("target_odds", 50.0)).orElse(50.0),
            formula = item.optString("formula").ifBlank { null },
            metrics = jsonObjects(item.optJSONArray("metrics")).map { metric ->
                ScorecardMetric(
                    name = metric.optString("name", "unnamed").ifBlank { "unnamed" },
                    formula = metric.optString("formula", "0").ifBlank { "0" },
                    unit = metric.optString("unit", ""),
                    category = metric.optString("category", "financial").ifBlank { "financial" },
                )
            },
            bins = jsonObjects(item.optJSONArray("bins")).map { factor ->
                ScoreFactor(
                    variableId = factor.optStringOrNull("variableId") ?: factor.optStringOrNull("variable_id"),
                    weight = factor.optDouble("weight", 1.0).orElse(1.0),
                    coefficient = factor.optDouble("coefficient", 0.0).orElse(0.0),
                    ranges = jsonObjects(factor.optJSONArray("ranges")).map { range ->
                        val lower = range.bound("min")
                        val upper = range.bound("max")
                        ScoreRange(
                            min = lower.value,
                            max = upper.value,
                            points = range.optInt("points"),
                            matchable = lower.matchable && upper.matchable,
                        )
                    },
                    woeValues = jsonObjects(factor.optJSONArray("woeValues").takeIf { it != null } ?: factor.optJSONArray("woe_values")).map { woe ->
                        val lower = woe.bound("min")
                        val upper = woe.bound("max")
                        WoeRange(
                            min = lower.value,
                            max = upper.value,
                            woe = woe.optDouble("woe", 0.0).orElse(0.0),
                            iv = woe.optDouble("iv", 0.0).orElse(0.0),
                            eventRate = woe.optDouble("eventRate", woe.optDouble("event_rate")).takeUnless { it.isNaN() },
                            nonEventRate = woe.optDouble("nonEventRate", woe.optDouble("non_event_rate")).takeUnless { it.isNaN() },
                            matchable = lower.matchable && upper.matchable,
                        )
                    },
                )
            },
        )
    }

    private fun JSONObject.optStringOrNull(key: String): String? =
        if (has(key) && !isNull(key)) optString(key).ifBlank { null } else null

    private class Bound(val value: Double?, val matchable: Boolean)

    /**
     * A bin bound, carrying the distinction the Python core makes. `float(candidate.get("min", 0))`
     * reads an ABSENT key as 0.0, but raises on an explicit null or a non-numeric value — and the
     * `except (TypeError, ValueError): continue` around it means such a candidate can never match.
     */
    private fun JSONObject.bound(key: String): Bound {
        if (!has(key)) return Bound(null, true)
        if (isNull(key)) return Bound(null, false)
        val raw = optDouble(key)
        return if (raw.isNaN()) Bound(null, false) else Bound(raw, true)
    }

    private fun Double.orElse(fallback: Double): Double = if (isNaN()) fallback else this

    private fun JSONArray?.toPolicies(): List<Policy> = jsonObjects(this).map { item ->
        Policy(
            id = item.optString("id"),
            name = item.optString("name"),
            steps = jsonObjects(item.optJSONArray("steps")).map { step ->
                PolicyStep(
                    id = step.optString("id").ifBlank { null },
                    type = step.optString("type"),
                    ref = step.optString("ref").ifBlank { null },
                    refId = step.optString("refId", step.optString("ref_id")).ifBlank { null },
                    label = step.optString("label", step.optString("name")).ifBlank { null },
                    config = step.optJSONObject("config")?.toMap() ?: emptyMap(),
                )
            },
            serverOnlySteps = item.optJSONArray("serverOnlySteps").toStringList(),
            defaultOutcome = item.optString("defaultOutcome", item.optString("default_outcome")).ifBlank { null },
        )
    }

    private fun JSONArray?.toExperiments(): List<Experiment> = jsonObjects(this).map { item ->
        Experiment(
            id = item.optString("id"),
            name = item.optString("name"),
            status = item.optString("status"),
            variants = jsonObjects(item.optJSONArray("variants")).map { variant ->
                ExperimentVariant(
                    id = variant.optString("id"),
                    weight = variant.optInt("weight"),
                    overrides = variant.optJSONObject("overrides")?.toMap() ?: emptyMap(),
                )
            },
            targetPolicyId = item.optString("targetPolicyId", item.optString("target_policy_id")).ifBlank { null },
        )
    }

    internal fun JSONObject.toMap(): Map<String, Any?> {
        val result = linkedMapOf<String, Any?>()
        val iterator = keys()
        while (iterator.hasNext()) {
            val key = iterator.next()
            val value = opt(key)
            result[key] = when (value) {
                JSONObject.NULL -> null
                is JSONObject -> value.toMap()
                is JSONArray -> value.toAnyList()
                else -> value
            }
        }
        return result
    }

    private fun JSONArray?.toMaps(): List<Map<String, Any?>> = jsonObjects(this).map { it.toMap() }

    private fun JSONArray?.toAnyList(): List<Any?> = jsonValues(this).map { value ->
        when (value) {
            JSONObject.NULL -> null
            is JSONObject -> value.toMap()
            is JSONArray -> value.toAnyList()
            else -> value
        }
    }

    private fun JSONArray?.toStringList(): List<String> = jsonValues(this).mapNotNull { value ->
        when (value) {
            null, JSONObject.NULL -> null
            else -> value.toString()
        }
    }

    private fun jsonObjects(array: JSONArray?): List<JSONObject> = jsonValues(array).mapNotNull { it as? JSONObject }

    private fun jsonValues(array: JSONArray?): List<Any?> {
        if (array == null) {
            return emptyList()
        }
        return buildList {
            for (index in 0 until array.length()) {
                add(array.opt(index))
            }
        }
    }
}
