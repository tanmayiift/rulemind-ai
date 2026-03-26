package com.rulemind.core

import com.rulemind.core.models.CompiledRule
import com.rulemind.core.models.RuleTreeNode

class RuleEvaluator {
    fun evaluate(rule: CompiledRule, variables: Map<String, Any?>): Map<String, Any?> {
        val conditions = mutableListOf<Map<String, Any?>>()
        val groups = mutableListOf<Map<String, Any?>>()
        val evaluation = if (rule.ruleFormat == "v2" && rule.tree != null) {
            evaluateTree(rule.tree, variables, conditions, groups)
        } else {
            evaluateFlat(rule.nodes, variables, conditions, groups)
        }
        return mapOf(
            "ruleId" to rule.id,
            "passed" to evaluation.passed,
            "outcome" to evaluation.outcome,
            "logic" to evaluation.logic,
            "conditions" to conditions,
            "groupResults" to groups,
        )
    }

    private fun evaluateFlat(
        nodes: List<Map<String, Any?>>,
        variables: Map<String, Any?>,
        conditions: MutableList<Map<String, Any?>>,
        groups: MutableList<Map<String, Any?>>,
    ): RuleEvaluation {
        val logic = if (nodes.any { (it["type"] as? String)?.lowercase() == "or" }) "OR" else "AND"
        val conditionResults = nodes.filter { (it["type"] as? String) == "condition" }.map { node ->
            val variableId = node["variable"] as? String ?: ""
            val actual = variables[variableId] ?: 0
            val operator = node["operator"] as? String ?: "=="
            val expected = node["value"]
            val passed = compare(actual, operator, expected)
            val result = mapOf(
                "variable_id" to variableId,
                "variable_name" to variableId,
                "operator" to operator,
                "threshold" to expected,
                "value" to actual,
                "passed" to passed,
                "group" to logic,
            )
            conditions += result
            passed
        }
        val passed = if (logic == "OR") conditionResults.any { it } else conditionResults.all { it }
        groups += mapOf("id" to "group", "logic" to logic, "passed" to passed, "childCount" to conditionResults.size)
        return RuleEvaluation(passed = passed, outcome = if (passed) "approve" else "review", logic = logic)
    }

    private fun evaluateTree(
        node: RuleTreeNode,
        variables: Map<String, Any?>,
        conditions: MutableList<Map<String, Any?>>,
        groups: MutableList<Map<String, Any?>>,
    ): RuleEvaluation {
        val passed = evaluateNode(node, variables, conditions, groups)
        return RuleEvaluation(
            passed = passed,
            outcome = if (passed) node.onPass ?: "approve" else node.onFail ?: "review",
            logic = node.logic ?: "AND",
        )
    }

    private fun evaluateNode(
        node: RuleTreeNode,
        variables: Map<String, Any?>,
        conditions: MutableList<Map<String, Any?>>,
        groups: MutableList<Map<String, Any?>>,
    ): Boolean {
        return when (node.type) {
            "condition" -> {
                val variableId = node.variable ?: ""
                val actual = variables[variableId] ?: 0
                val operator = node.operator ?: "=="
                val expected = node.value
                val passed = compare(actual, operator, expected)
                conditions += mapOf(
                    "variable_id" to variableId,
                    "variable_name" to variableId,
                    "operator" to operator,
                    "threshold" to expected,
                    "value" to actual,
                    "passed" to passed,
                    "group" to (node.logic ?: "AND"),
                )
                passed
            }

            "not" -> {
                val child = node.child ?: node.children.firstOrNull() ?: return false
                val passed = !evaluateNode(child, variables, conditions, groups)
                groups += mapOf("id" to (node.id ?: "not"), "logic" to "NOT", "passed" to passed, "childCount" to 1)
                passed
            }

            else -> {
                val logic = (node.logic ?: "AND").uppercase()
                val children = node.children.map { evaluateNode(it, variables, conditions, groups) }
                val passed = if (logic == "OR") children.any { it } else children.all { it }
                groups += mapOf("id" to (node.id ?: "group"), "logic" to logic, "passed" to passed, "childCount" to children.size)
                passed
            }
        }
    }

    private fun compare(actual: Any?, operator: String, expected: Any?): Boolean {
        val actualValue = if (actual is Number) actual.toDouble() else (actual?.toString()?.toDoubleOrNull() ?: 0.0)
        val expectedValue = when (expected) {
            is Number -> expected.toDouble()
            is String -> expected.toDoubleOrNull()
            else -> null
        }
        return when (operator) {
            ">" -> actualValue > (expectedValue ?: 0.0)
            ">=" -> actualValue >= (expectedValue ?: 0.0)
            "<" -> actualValue < (expectedValue ?: 0.0)
            "<=" -> actualValue <= (expectedValue ?: 0.0)
            "!=" -> actual != expected
            else -> actual == expected || actualValue == (expectedValue ?: Double.NaN)
        }
    }

    private data class RuleEvaluation(
        val passed: Boolean,
        val outcome: String,
        val logic: String,
    )
}
