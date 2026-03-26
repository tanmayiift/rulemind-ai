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
            defaultValue = item.opt("defaultValue").takeUnless { it == JSONObject.NULL },
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
            bins = jsonObjects(item.optJSONArray("bins")).map { factor ->
                ScoreFactor(
                    variableId = factor.optString("variableId", factor.optString("variable_id")),
                    ranges = jsonObjects(factor.optJSONArray("ranges")).map { range ->
                        ScoreRange(
                            min = range.optDouble("min").takeUnless { it.isNaN() },
                            max = range.optDouble("max").takeUnless { it.isNaN() },
                            points = range.optInt("points"),
                        )
                    },
                )
            },
        )
    }

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
