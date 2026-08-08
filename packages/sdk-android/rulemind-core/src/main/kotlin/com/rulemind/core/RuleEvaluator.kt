package com.rulemind.core

import com.rulemind.core.models.CompiledRule
import com.rulemind.core.models.RuleTreeNode

class RuleEvaluator {
    companion object {
        // Mirrors app/logic.py MAX_RULE_TREE_DEPTH.
        const val MAX_RULE_TREE_DEPTH = 200
    }

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
            // A MISSING variable stays null (not 0) so numeric comparisons return false,
            // matching the Python core (compare(None, ...) -> False). Defaulting to 0 made
            // `missing < 700` true on-device but false server-side — a real divergence.
            val actual = variables[variableId]
            val operator = node["operator"] as? String ?: "=="
            val expected = node["value"]
            val passed = compare(actual, operator, expected, node["value2"], node["fieldType"] as? String)
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
        val passed = evaluateNode(node, variables, conditions, groups, 0)
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
        depth: Int = 0,
    ): Boolean {
        // Guard pathological nesting from a native stack overflow (matches the Python core).
        if (depth > MAX_RULE_TREE_DEPTH) {
            throw IllegalStateException("Rule tree nesting exceeds the maximum depth ($MAX_RULE_TREE_DEPTH)")
        }
        return when (node.type) {
            "condition" -> {
                val variableId = node.variable ?: ""
                // Missing variable -> null (not 0), matching the Python core. See evaluateFlat.
                val actual = variables[variableId]
                val operator = node.operator ?: "=="
                val expected = node.value
                val passed = compare(actual, operator, expected, node.value2, node.fieldType)
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
                val passed = !evaluateNode(child, variables, conditions, groups, depth + 1)
                groups += mapOf("id" to (node.id ?: "not"), "logic" to "NOT", "passed" to passed, "childCount" to 1)
                passed
            }

            else -> {
                val logic = (node.logic ?: "AND").uppercase()
                val children = node.children.map { evaluateNode(it, variables, conditions, groups, depth + 1) }
                val passed = if (logic == "OR") children.any { it } else children.all { it }
                groups += mapOf("id" to (node.id ?: "group"), "logic" to logic, "passed" to passed, "childCount" to children.size)
                passed
            }
        }
    }

    /**
     * Public entry point used by the cross-engine operator conformance suite
     * (packages/shared/operators.spec.json). Delegates to the private [compare].
     */
    fun compareCondition(
        actual: Any?,
        operator: String,
        expected: Any?,
        expected2: Any? = null,
        fieldType: String? = null,
    ): Boolean = compare(actual, operator, expected, expected2, fieldType)

    /**
     * Evaluate a single condition. Mirrors the canonical operator contract
     * (packages/shared/operators.spec.json) shared with the TS, Python, and
     * Dart engines: == != > >= < <= between in not_in regex exists !exists.
     */
    private fun compare(
        actual: Any?,
        operator: String,
        expected: Any?,
        expected2: Any? = null,
        fieldType: String? = null,
    ): Boolean {
        // Existence checks run before any missing-value handling.
        if (operator == "exists") return actual != null && actual != ""
        if (operator == "!exists") return actual == null || actual == ""

        // Set membership. `expected` may be a list or a comma-separated string.
        if (operator == "in" || operator == "not_in") {
            val options = asOptionList(expected)
            val matched = options.any { looseEqual(actual, it) }
            return if (operator == "in") matched else !matched
        }

        // Regular-expression match against the string form of the value.
        if (operator == "regex") {
            if (actual == null) return false
            return try {
                Regex(expected.toString()).containsMatchIn(actual.toString())
            } catch (_: Exception) {
                false
            }
        }

        // Boolean-typed equality (only when the field is declared boolean).
        if ((fieldType ?: "").lowercase() == "boolean" && (operator == "==" || operator == "!=")) {
            val matched = toBool(actual) == toBool(expected)
            return if (operator == "==") matched else !matched
        }

        // Date-typed comparison: normalize both sides to a UTC epoch so dates are
        // ORDERED and equality is spelling-insensitive. Non-ISO/out-of-range -> false.
        if ((fieldType ?: "").lowercase() == "date") {
            val a = dateToEpoch(actual) ?: return false
            val e = dateToEpoch(expected) ?: return false
            return when (operator) {
                "==" -> a == e
                "!=" -> a != e
                ">=" -> a >= e
                "<=" -> a <= e
                ">" -> a > e
                "<" -> a < e
                "between" -> { val u = dateToEpoch(expected2); u != null && a in e..u }
                else -> false
            }
        }

        // Ordered comparisons and inclusive range operate on numbers (non-date).
        if (operator in setOf(">=", "<=", ">", "<", "between")) {
            val a = numericOrNull(actual) ?: return false
            val e = numericOrNull(expected) ?: return false
            return when (operator) {
                ">=" -> a >= e
                "<=" -> a <= e
                ">" -> a > e
                "<" -> a < e
                else -> {
                    val upper = numericOrNull(expected2) ?: return false
                    a in e..upper
                }
            }
        }

        return when (operator) {
            "==" -> looseEqual(actual, expected)
            "!=" -> !looseEqual(actual, expected)
            else -> false
        }
    }

    private fun numericOrNull(value: Any?): Double? {
        // Non-finite (NaN / ±Infinity) -> null so "Infinity" can't clear a numeric gate.
        val n = when (value) {
            is Number -> value.toDouble()
            is String -> value.toDoubleOrNull()
            else -> null
        }
        return if (n != null && n.isFinite()) n else null
    }

    private fun toBool(value: Any?): Boolean = when (value) {
        is Boolean -> value
        is Number -> value.toDouble() != 0.0
        else -> value?.toString()?.trim()?.lowercase() in setOf("true", "1", "yes")
    }

    private fun asOptionList(expected: Any?): List<Any?> = when (expected) {
        is List<*> -> expected
        null -> emptyList()
        else -> expected.toString().split(",").map { it.trim() }.filter { it.isNotEmpty() }
    }

    private fun looseEqual(a: Any?, b: Any?): Boolean {
        if (a == b) return true
        val an = numericOrNull(a)
        val bn = numericOrNull(b)
        if (an != null && bn != null) return an == bn
        return a.toString() == b.toString()
    }

    // First-class date type — ISO date/date-time (UTC) to epoch seconds via integer
    // civil-days math (Howard Hinnant), identical to the Python/TS/Rust/Dart engines.
    private val daysInMonth = intArrayOf(31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

    private fun isLeapYear(y: Long): Boolean = (y % 4 == 0L && y % 100 != 0L) || y % 400 == 0L

    private fun daysFromCivil(year: Long, month: Long, day: Long): Long {
        val y = year - if (month <= 2) 1 else 0
        val era = (if (y >= 0) y else y - 399) / 400
        val yoe = y - era * 400
        val doy = (153 * (month + if (month > 2) -3 else 9) + 2) / 5 + (day - 1)
        val doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
        return era * 146097 + doe - 719468
    }

    private fun dateToEpoch(value: Any?): Long? {
        if (value == null || value is Boolean) return null
        val s = value.toString().trim()
        if (s.isEmpty()) return null
        val sep = s.indexOfFirst { it == 'T' || it == ' ' }
        val datePart = if (sep >= 0) s.substring(0, sep) else s
        val timePart = if (sep >= 0) s.substring(sep + 1) else null
        val dparts = datePart.split("-")
        if (dparts.size != 3 || dparts[0].length != 4) return null
        val year = dparts[0].toLongOrNull() ?: return null
        val month = dparts[1].toLongOrNull() ?: return null
        val day = dparts[2].toLongOrNull() ?: return null
        var hour = 0L
        var minute = 0L
        var second = 0L
        var offsetSecs = 0L // timezone offset so +05:30 and Z resolve to the same UTC epoch
        if (timePart != null) {
            var tp: String
            var tz: String? = null
            val zIdx = timePart.indexOf('Z')
            val signIdx = timePart.indexOfLast { it == '+' || it == '-' }
            if (zIdx >= 0) {
                tp = timePart.substring(0, zIdx)
            } else if (signIdx >= 0) {
                tp = timePart.substring(0, signIdx)
                tz = timePart.substring(signIdx)
            } else {
                tp = timePart
            }
            val dot = tp.indexOf('.')
            if (dot >= 0) tp = tp.substring(0, dot)
            val tparts = tp.split(":")
            if (tparts.size < 2 || tparts.size > 3) return null
            hour = tparts[0].toLongOrNull() ?: return null
            minute = tparts[1].toLongOrNull() ?: return null
            if (tparts.size == 3) second = tparts[2].toLongOrNull() ?: return null
            if (tz != null) {
                val digits = tz.filter { it.isDigit() }
                if (digits.length != 4) return null
                val magnitude = digits.substring(0, 2).toLong() * 3600 + digits.substring(2, 4).toLong() * 60
                offsetSecs = if (tz.startsWith("+")) -magnitude else magnitude
            }
        }
        if (month !in 1..12) return null
        val dim = if (month == 2L && isLeapYear(year)) 29L else daysInMonth[(month - 1).toInt()].toLong()
        if (day !in 1..dim) return null
        if (hour !in 0..23 || minute !in 0..59 || second !in 0..59) return null
        return daysFromCivil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second + offsetSecs
    }

    private data class RuleEvaluation(
        val passed: Boolean,
        val outcome: String,
        val logic: String,
    )
}
